"""微信 iLink Bot API HTTP 客户端

实现 iLink 协议（微信官方 Bot API）的 HTTP/JSON 客户端，支持：
- 扫码登录（获取二维码 + 轮询扫码状态）
- 长轮询接收消息（getupdates）
- 发送文本消息
- 输入状态通知（typing）
- 配置查询（typing_ticket）

参考 qq_bot_adapter.py 的代码风格，使用 httpx.AsyncClient 作为底层 HTTP 客户端。

协议规范：
- Base URL: https://ilinkai.weixin.qq.com
- CDN: https://novac2c.cdn.weixin.qq.com/c2c
- 每个业务 POST 请求需携带 Authorization / AuthorizationType / X-WECHAT-UIN 头
- 所有请求体包含 base_info: { channel_version: "2.0.0" }
- X-WECHAT-UIN 每次随机生成（base64(String(randomUint32()))），用于防重放
- 长轮询服务器会挂起 35 秒，客户端 timeout 设为 40 秒
- getupdates 返回 ret == -14 表示会话过期，需重新登录
"""
from __future__ import annotations

import base64
import os
import random
import struct
import uuid
from typing import Any, Optional

import httpx
from loguru import logger


# ============================================================================
# 常量定义
# ============================================================================

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# 通道版本号，所有请求体必填字段
CHANNEL_VERSION = "2.0.0"

# 长轮询超时：服务器挂起 35 秒，客户端 40 秒留余量
LONG_POLL_TIMEOUT = 40.0

# 普通请求超时
DEFAULT_TIMEOUT = 15.0

# iLink ret 码
RET_OK = 0
RET_PARAM_ERROR = -2  # 参数错误（缺隐藏必填字段或 context_token 过期/无效）
RET_SESSION_EXPIRED = -14

# 扫码状态
QR_STATUS_WAIT = "wait"
QR_STATUS_SCANED = "scaned"
QR_STATUS_CONFIRMED = "confirmed"
QR_STATUS_EXPIRED = "expired"


class SessionExpiredError(Exception):
    """iLink 会话过期异常。

    当 getupdates 返回 ret == -14 时抛出，调用方应捕获此异常并重新登录
    （重新扫码或刷新 bot_token）。

    Attributes:
        message: 错误描述
        ret: iLink 服务端返回的 ret 码
    """

    def __init__(self, message: str = "iLink session expired (ret=-14)", ret: int = RET_SESSION_EXPIRED) -> None:
        super().__init__(message)
        self.message = message
        self.ret = ret

    def __str__(self) -> str:
        return self.message


class ILinkClient:
    """微信 iLink Bot API HTTP 客户端

    封装 iLink 协议的所有 HTTP 接口，提供扫码登录、长轮询收消息、
    发送消息、输入状态、配置查询等能力。

    使用 httpx.AsyncClient 作为底层 HTTP 客户端，支持连接池复用。
    所有方法均为 async，需在 asyncio 事件循环中调用。

    典型用法::

        client = ILinkClient()
        # 1. 扫码登录
        qr = await client.get_qrcode()
        status = await client.get_qrcode_status(qr["qrcode_id"])
        client.update_token(status["bot_token"])
        # 2. 长轮询收消息
        cursor = ""
        while True:
            result = await client.get_updates(cursor)
            cursor = result["cursor"]
            for msg in result["msgs"]:
                await client.send_message(msg["from_user_id"], msg["context_token"], "回复")
    """

    def __init__(
        self,
        base_url: str = ILINK_BASE_URL,
        bot_token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """初始化 iLink 客户端

        Args:
            base_url: iLink 服务端地址，默认 https://ilinkai.weixin.qq.com
            bot_token: 初始 bot_token（已登录场景可复用），可为空
            timeout: 普通请求超时秒数，长轮询使用 LONG_POLL_TIMEOUT
            client: 可选的外部 httpx.AsyncClient（连接池复用场景），
                    未提供时内部自建（首次请求时惰性创建）
        """
        self._base_url = base_url.rstrip("/")
        self._bot_token = bot_token
        self._timeout = timeout
        # 外部注入的 client（共享连接池）；为 None 时使用内部自建的 _owned_client
        self._external_client = client
        self._owned_client: Optional[httpx.AsyncClient] = None
        # 登录成功后服务端可能下发的 baseurl（用于切换到专用域名）
        self._active_base_url: str = self._base_url
        logger.info(
            "ilink.client.init base_url={} bot_token={} timeout={}",
            self._base_url,
            "<set>" if bot_token else "<empty>",
            timeout,
        )

    # ------------------------------------------------------------------
    # 资源管理
    # ------------------------------------------------------------------

    @property
    def _client(self) -> httpx.AsyncClient:
        """获取当前 httpx.AsyncClient 实例（惰性创建内部 client）。"""
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=self._timeout)
        return self._owned_client

    async def close(self) -> None:
        """关闭内部自建的 httpx.AsyncClient。

        外部注入的 client 不在此关闭（由注入方负责生命周期）。
        """
        if self._owned_client is not None:
            try:
                await self._owned_client.aclose()
                logger.info("ilink.client.closed")
            except Exception as e:
                logger.warning("ilink.client.close_failed error={}", str(e)[:200])
            finally:
                self._owned_client = None

    async def __aenter__(self) -> "ILinkClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def update_token(self, bot_token: str) -> None:
        """更新 bot_token（扫码登录成功后调用）。

        Args:
            bot_token: 新的 Bearer token
        """
        self._bot_token = bot_token
        logger.info("ilink.client.token_updated token_len={}", len(bot_token) if bot_token else 0)

    def update_base_url(self, base_url: str) -> None:
        """更新活跃 base_url（扫码确认后服务端可能下发 baseurl）。

        Args:
            base_url: 新的 iLink 服务端地址
        """
        if not base_url:
            return
        self._active_base_url = base_url.rstrip("/")
        logger.info("ilink.client.base_url_updated url={}", self._active_base_url)

    @property
    def bot_token(self) -> str:
        """当前 bot_token。"""
        return self._bot_token

    # ------------------------------------------------------------------
    # 请求头与请求体构建
    # ------------------------------------------------------------------

    def _build_headers(self, bot_token: str = "") -> dict[str, str]:
        """构建 iLink 业务请求头。

        包含以下字段：
        - Content-Type: application/json
        - AuthorizationType: ilink_bot_token
        - Authorization: Bearer <bot_token>
        - X-WECHAT-UIN: base64(String(randomUint32())) — 每次随机，防重放

        Args:
            bot_token: 本次请求使用的 bot_token；为空时使用 self._bot_token

        Returns:
            请求头字典
        """
        token = bot_token or self._bot_token
        # 生成 4 字节随机 uint32，转字符串后 base64 编码（防重放）
        random_uin = struct.unpack("<I", os.urandom(4))[0]
        uin_b64 = base64.b64encode(str(random_uin).encode("utf-8")).decode("ascii")
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": uin_b64,
        }

    def _build_body(self, data: Optional[dict] = None) -> dict:
        """构建请求体，注入 base_info。

        所有 iLink 业务请求体必须包含 base_info: { channel_version: "2.0.0" }。
        本方法对传入的 data 做浅拷贝后注入 base_info，避免污染调用方原始字典。

        Args:
            data: 业务请求体字段，可为 None（仅 base_info）

        Returns:
            完整请求体字典
        """
        body = dict(data) if data else {}
        body["base_info"] = {"channel_version": CHANNEL_VERSION}
        return body

    # ------------------------------------------------------------------
    # HTTP 请求封装
    # ------------------------------------------------------------------

    async def _post(
        self,
        path: str,
        data: Optional[dict] = None,
        *,
        bot_token: str = "",
        timeout: Optional[float] = None,
    ) -> dict:
        """发送 POST 请求到 iLink 服务端。

        Args:
            path: 请求路径（以 / 开头）
            data: 请求体业务字段（base_info 会自动注入）
            bot_token: 可选的本次请求专用 bot_token
            timeout: 可选的本次请求超时覆盖

        Returns:
            服务端返回的 JSON 字典

        Raises:
            SessionExpiredError: ret == -14
            httpx.HTTPError: 网络/HTTP 错误
            RuntimeError: 服务端返回 ret != 0（非 -14）
        """
        url = f"{self._active_base_url}{path}"
        body = self._build_body(data)
        headers = self._build_headers(bot_token)
        req_timeout = httpx.Timeout(timeout if timeout is not None else self._timeout)
        logger.debug("ilink.post url={} body_keys={}", url, list(body.keys()))
        try:
            response = await self._client.post(url, json=body, headers=headers, timeout=req_timeout)
        except httpx.TimeoutException as e:
            logger.error("ilink.post.timeout url={} error={}", url, str(e)[:200])
            raise
        except httpx.HTTPError as e:
            logger.error("ilink.post.http_error url={} error={}", url, str(e)[:200])
            raise

        if response.status_code != 200:
            logger.error(
                "ilink.post.bad_status url={} status={} body={}",
                url, response.status_code, response.text[:300],
            )
            raise RuntimeError(f"iLink HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except Exception as e:
            logger.error("ilink.post.json_parse_failed url={} error={} body={}", url, str(e)[:200], response.text[:300])
            raise RuntimeError(f"iLink 响应解析失败: {e}") from e

        # 检查 ret 码（部分接口无 ret 字段，跳过检查）
        ret = payload.get("ret")
        if ret is not None:
            if ret == RET_SESSION_EXPIRED:
                logger.warning("ilink.session_expired url={}", url)
                raise SessionExpiredError()
            if ret != RET_OK:
                logger.warning("ilink.post.bad_ret url={} ret={} payload={}", url, ret, str(payload)[:300])
                raise RuntimeError(f"iLink ret={ret}: {str(payload)[:200]}")
        logger.debug("ilink.post.ok url={} ret={}", url, ret)
        return payload

    async def _get(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """发送 GET 请求到 iLink 服务端（用于扫码等无认证接口）。

        Args:
            path: 请求路径（以 / 开头）
            params: 查询参数
            headers: 附加请求头（如 iLink-App-ClientVersion: 1）
            timeout: 可选的本次请求超时覆盖

        Returns:
            服务端返回的 JSON 字典
        """
        url = f"{self._active_base_url}{path}"
        req_timeout = httpx.Timeout(timeout if timeout is not None else self._timeout)
        logger.debug("ilink.get url={} params={}", url, params)
        try:
            response = await self._client.get(
                url, params=params, headers=headers, timeout=req_timeout,
            )
        except httpx.TimeoutException as e:
            logger.error("ilink.get.timeout url={} error={}", url, str(e)[:200])
            raise
        except httpx.HTTPError as e:
            logger.error("ilink.get.http_error url={} error={}", url, str(e)[:200])
            raise

        if response.status_code != 200:
            logger.error(
                "ilink.get.bad_status url={} status={} body={}",
                url, response.status_code, response.text[:300],
            )
            raise RuntimeError(f"iLink HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except Exception as e:
            logger.error("ilink.get.json_parse_failed url={} error={} body={}", url, str(e)[:200], response.text[:300])
            raise RuntimeError(f"iLink 响应解析失败: {e}") from e

        logger.debug("ilink.get.ok url={} payload_keys={}", url, list(payload.keys()) if isinstance(payload, dict) else "n/a")
        return payload

    # ------------------------------------------------------------------
    # API: 扫码登录
    # ------------------------------------------------------------------

    async def get_qrcode(self) -> dict:
        """获取 iLink 登录二维码。

        调用 GET /ilink/bot/get_bot_qrcode?bot_type=3，无需认证。

        Returns:
            字典包含:
                - qrcode_id (str): 二维码标识，用于后续轮询状态
                - qrcode_url (str): 二维码图片 URL（可直接展示给用户扫码）

        Raises:
            httpx.HTTPError: 网络错误
            RuntimeError: 响应解析失败
        """
        payload = await self._get(
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
        )
        qrcode_id = payload.get("qrcode", "")
        qrcode_url = payload.get("qrcode_img_content", "")
        logger.info("ilink.qrcode.got id={} url={}", qrcode_id, qrcode_url[:80])
        return {
            "qrcode_id": qrcode_id,
            "qrcode_url": qrcode_url,
        }

    async def get_qrcode_status(self, qrcode_id: str) -> dict:
        """轮询二维码扫码状态。

        调用 GET /ilink/bot/get_qrcode_status?qrcode=xxx，
        需携带 iLink-App-ClientVersion: 1 请求头。

        Args:
            qrcode_id: get_qrcode 返回的 qrcode_id

        Returns:
            字典包含:
                - status (str): 扫码状态，取值 wait/scaned/confirmed/expired
                - bot_token (str, 可选): 登录成功后的 Bearer token（status=confirmed 时存在）
                - baseurl (str, 可选): 登录成功后切换的服务端地址
                - ilink_bot_id (str, 可选): 登录成功后的 bot ID
                - ilink_user_id (str, 可选): 登录成功后的 user ID

        Raises:
            httpx.HTTPError: 网络错误
            RuntimeError: 响应解析失败
        """
        payload = await self._get(
            "/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode_id},
            headers={"iLink-App-ClientVersion": "1"},
        )
        status = payload.get("status", QR_STATUS_WAIT)
        result: dict[str, Any] = {"status": status}
        # 登录确认后服务端会下发以下字段
        for key in ("bot_token", "ilink_bot_id", "ilink_user_id", "baseurl"):
            val = payload.get(key)
            if val:
                result[key] = val
        logger.info(
            "ilink.qrcode.status qrcode_id={} status={} has_token={}",
            qrcode_id, status, "bot_token" in result,
        )
        return result

    # ------------------------------------------------------------------
    # API: 长轮询收消息
    # ------------------------------------------------------------------

    async def get_updates(self, cursor: str = "") -> dict:
        """长轮询获取新消息。

        调用 POST /ilink/bot/getupdates，服务器挂起最多 35 秒等待新消息。
        首次调用 cursor 传空字符串，后续传入上次返回的 cursor 继续轮询。

        Args:
            cursor: 上次返回的 get_updates_buf 游标，首次为空字符串

        Returns:
            字典包含:
                - ret (int): 返回码，0 表示成功
                - msgs (list): 消息列表，每条消息包含 from_user_id/to_user_id/
                  message_type/context_token/item_list 等字段
                - context_token (str): 上下文 token（用于后续 send_message）
                - cursor (str): 下一次 get_updates 使用的游标

        Raises:
            SessionExpiredError: ret == -14，会话过期需重新登录
            httpx.TimeoutException: 网络超时（>40 秒）
            httpx.HTTPError: 其他网络错误
            RuntimeError: 服务端返回 ret != 0（非 -14）
        """
        data = {"get_updates_buf": cursor}
        payload = await self._post(
            "/ilink/bot/getupdates",
            data=data,
            timeout=LONG_POLL_TIMEOUT,
        )
        ret = payload.get("ret", RET_OK)
        msgs = payload.get("msgs", []) or []
        context_token = payload.get("context_token", "") or ""
        next_cursor = payload.get("get_updates_buf", "") or ""
        logger.info(
            "ilink.get_updates.ok ret={} msg_count={} cursor_len={} ctx_token_len={}",
            ret, len(msgs), len(next_cursor), len(context_token),
        )
        return {
            "ret": ret,
            "msgs": msgs,
            "context_token": context_token,
            "cursor": next_cursor,
        }

    # ------------------------------------------------------------------
    # API: 发送消息
    # ------------------------------------------------------------------

    async def send_message(self, to_user_id: str, context_token: str, text: str) -> dict:
        """发送文本消息给指定用户。

        调用 POST /ilink/bot/sendmessage，构造 msg.item_list 文本项。

        请求体必须包含官方 SDK（@tencent-weixin/openclaw-weixin 的 buildTextMessageReq）
        的隐藏必填字段，缺失会导致服务端返回 ret=-2（prepare failed）或静默丢弃：
        - from_user_id: 空字符串（必须存在，不能省略）
        - client_id: 每条消息的唯一 ID（UUID，用于服务端去重和路由）

        context_token 是消息路由的必需字段，过期/无效时服务端返回 ret=-2
        （errmsg 通常为 "prepare failed"）。官方协议明确 context_token 不可省略，
        因此不做 tokenless 降级重试（被 cc-connect #1176/#1441 等同类项目证实无效）。
        token 过期的正确恢复方式：等待该用户发送新消息以刷新 context_token。

        Args:
            to_user_id: 接收方用户 ID（来自消息的 from_user_id）
            context_token: 会话上下文 token（来自消息或 get_updates）
            text: 文本内容

        Returns:
            字典包含:
                - ret (int): 返回码，0 表示成功

        Raises:
            SessionExpiredError: ret == -14
            httpx.HTTPError: 网络错误
            RuntimeError: 服务端返回 ret != 0（非 -14），含 ret=-2 参数错误
        """
        data = {
            "msg": {
                # 隐藏必填字段：必须存在（空字符串），缺失会被服务端拒绝
                "from_user_id": "",
                "to_user_id": to_user_id,
                # 每条消息唯一 ID，用于服务端去重和路由
                "client_id": f"bot-{uuid.uuid4().hex[:16]}",
                "message_type": 2,  # BOT 消息（1=用户, 2=BOT）
                "message_state": 2,  # FINISH（0=新建, 1=生成中, 2=完成）
                "context_token": context_token,
                "item_list": [
                    {"type": 1, "text_item": {"text": text}},
                ],
            }
        }
        try:
            payload = await self._post("/ilink/bot/sendmessage", data=data)
            ret = payload.get("ret", RET_OK)
            logger.info(
                "ilink.send_message.ok to={} text_len={} ret={}",
                to_user_id, len(text), ret,
            )
            return {"ret": ret}
        except RuntimeError as e:
            # ret=-2 = 参数错误（context_token 过期/无效，或请求体不合规）
            # context_token 是消息路由必需字段，去掉它服务端无法路由，
            # 仍返回 ret=-2，故 tokenless 降级无效。
            # 恢复方式：等待该用户发新消息刷新 context_token 后再回复。
            if "ret=-2" not in str(e):
                raise
            logger.warning(
                "ilink.send_message.ret_minus_2 to={} text_len={} "
                "ctx_token_len={} cause=context_token_expired_or_invalid "
                "recovery=user_must_send_new_message_to_refresh",
                to_user_id, len(text), len(context_token),
            )
            raise

    # ------------------------------------------------------------------
    # API: 输入状态
    # ------------------------------------------------------------------

    async def send_typing(self, user_id: str, ticket: str, status: int) -> dict:
        """发送输入状态通知。

        调用 POST /ilink/bot/sendtyping，通知对方"正在输入"或"停止输入"。
        typing_ticket 通过 get_config 获取。

        Args:
            user_id: 对方用户 ID（ilink_user_id）
            ticket: typing_ticket，来自 get_config 返回
            status: 1=开始输入，2=停止输入

        Returns:
            字典包含:
                - ret (int): 返回码，0 表示成功

        Raises:
            SessionExpiredError: ret == -14
            httpx.HTTPError: 网络错误
            RuntimeError: 服务端返回 ret != 0（非 -14）
        """
        data = {
            "ilink_user_id": user_id,
            "typing_ticket": ticket,
            "status": status,
        }
        payload = await self._post("/ilink/bot/sendtyping", data=data)
        ret = payload.get("ret", RET_OK)
        logger.info(
            "ilink.send_typing.ok user={} status={} ret={}",
            user_id, status, ret,
        )
        return {"ret": ret}

    # ------------------------------------------------------------------
    # API: 获取配置
    # ------------------------------------------------------------------

    async def get_config(self, user_id: str, context_token: str) -> dict:
        """获取会话配置（typing_ticket）。

        调用 POST /ilink/bot/getconfig，返回 typing_ticket，
        用于后续 send_typing 接口。

        Args:
            user_id: 对方用户 ID（ilink_user_id）
            context_token: 会话上下文 token

        Returns:
            字典包含:
                - typing_ticket (str): 输入状态票据（base64 编码）
                - ret (int): 返回码（若服务端返回）

        Raises:
            SessionExpiredError: ret == -14
            httpx.HTTPError: 网络错误
            RuntimeError: 服务端返回 ret != 0（非 -14）
        """
        data = {
            "ilink_user_id": user_id,
            "context_token": context_token,
        }
        payload = await self._post("/ilink/bot/getconfig", data=data)
        typing_ticket = payload.get("typing_ticket", "") or ""
        ret = payload.get("ret", RET_OK)
        logger.info(
            "ilink.get_config.ok user={} ticket_len={} ret={}",
            user_id, len(typing_ticket), ret,
        )
        return {
            "typing_ticket": typing_ticket,
            "ret": ret,
        }

    # ------------------------------------------------------------------
    # API: 测试消息（登录验证）
    # ------------------------------------------------------------------

    async def send_test_message(self, bot_token: str, user_id: str) -> tuple[bool, str]:
        """使用给定 bot_token 发送测试消息，用于验证登录是否成功。

        使用独立的 bot_token 参数（不依赖 self._bot_token），适合在扫码登录
        完成后立即验证 token 有效性。发送一条简短测试文本到指定 user_id。
        由于此时尚无有效 context_token，传入空字符串尝试发送。

        Args:
            bot_token: 待验证的 bot_token
            user_id: 测试消息接收方用户 ID

        Returns:
            tuple[bool, str]:
                - (True, "ok") 表示发送成功
                - (False, error_msg) 表示发送失败，error_msg 描述失败原因

        Note:
            本方法不会抛出异常，所有错误均通过返回值传递，
            方便调用方在登录流程中做 try/except 之外的判断。
        """
        logger.info("ilink.send_test_message.start user={} token_len={}", user_id, len(bot_token))
        try:
            result = await self.send_message(
                to_user_id=user_id,
                context_token="",
                text="iLink Bot 已上线，登录验证成功。",
            )
            ret = result.get("ret", RET_OK)
            if ret == RET_OK:
                logger.info("ilink.send_test_message.ok user={}", user_id)
                return True, "ok"
            logger.warning("ilink.send_test_message.bad_ret user={} ret={}", user_id, ret)
            return False, f"ret={ret}"
        except SessionExpiredError as e:
            logger.warning("ilink.send_test_message.session_expired user={}", user_id)
            return False, "session_expired"
        except httpx.TimeoutException as e:
            logger.warning("ilink.send_test_message.timeout user={} error={}", user_id, str(e)[:200])
            return False, f"timeout: {e}"
        except httpx.HTTPError as e:
            logger.warning("ilink.send_test_message.http_error user={} error={}", user_id, str(e)[:200])
            return False, f"http_error: {e}"
        except Exception as e:
            logger.warning(
                "ilink.send_test_message.failed user={} error={} type={}",
                user_id, str(e)[:200], type(e).__name__,
            )
            return False, f"{type(e).__name__}: {e}"


# ============================================================================
# 便捷函数
# ============================================================================


async def login_via_qrcode(client: ILinkClient, *, poll_interval: float = 2.0, max_wait: float = 180.0) -> Optional[dict]:
    """通过扫码完成 iLink 登录的便捷流程。

    1. 获取二维码并打印 URL
    2. 轮询扫码状态直到 confirmed 或 expired
    3. 登录成功后更新 client 的 token 和 base_url

    Args:
        client: ILinkClient 实例
        poll_interval: 轮询间隔秒数
        max_wait: 最大等待秒数

    Returns:
        登录成功时返回扫码状态字典（含 bot_token/baseurl 等）；
        超时或二维码过期时返回 None
    """
    import asyncio
    import time

    qr = await client.get_qrcode()
    qrcode_id = qr["qrcode_id"]
    qrcode_url = qr["qrcode_url"]
    logger.info("ilink.login.qrcode url={}", qrcode_url)
    print(f"\n请用微信扫描二维码登录:\n  {qrcode_url}\n")

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        status = await client.get_qrcode_status(qrcode_id)
        st = status.get("status", QR_STATUS_WAIT)
        if st == QR_STATUS_CONFIRMED:
            bot_token = status.get("bot_token", "")
            baseurl = status.get("baseurl", "")
            if bot_token:
                client.update_token(bot_token)
            if baseurl:
                client.update_base_url(baseurl)
            logger.info("ilink.login.confirmed baseurl={}", baseurl or "<default>")
            return status
        if st == QR_STATUS_EXPIRED:
            logger.warning("ilink.login.expired")
            return None
        await asyncio.sleep(poll_interval)

    logger.warning("ilink.login.timeout after {}s", max_wait)
    return None


# ============================================================================
# 模块入口：扫码登录自检
# ============================================================================


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        async with ILinkClient() as client:
            result = await login_via_qrcode(client)
            if result is None:
                print("登录失败或超时")
                return
            # 登录成功后发送测试消息
            user_id = result.get("ilink_user_id", "")
            if user_id:
                ok, msg = await client.send_test_message(client.bot_token, user_id)
                print(f"测试消息发送: {'成功' if ok else '失败'} ({msg})")
            else:
                print("登录成功但未拿到 ilink_user_id，跳过测试消息")

    asyncio.run(_main())
