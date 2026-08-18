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
import hashlib
import asyncio
import os
import random
import struct
import time
import urllib.parse
import uuid
from typing import Any, Optional

import httpx
from loguru import logger

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding


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


def _normalize_base_url(url: str) -> str:
    """仅允许 HTTPS 的 iLink 服务端地址，非 HTTPS 时回退官方默认域名。

    防止 Bearer bot_token 通过明文 HTTP 传输（CWE-319）。
    """
    url = (url or "").strip().rstrip("/")
    if url.lower().startswith("https://"):
        return url
    if url:
        logger.warning("ilink.client.rejected_non_https_url url={}", url[:200])
    return ILINK_BASE_URL


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


class ILinkRetError(RuntimeError):
    """iLink 业务错误异常，携带 ret 码供上层精确判断。

    当服务端返回 ret != 0 且 ret != -14 时抛出。
    调用方可通过 e.ret 直接判断错误类型，无需解析字符串。

    Attributes:
        ret: iLink 服务端返回的 ret 码
        payload: 完整的响应 payload（调试用）
    """

    def __init__(self, ret: int, payload: dict | None = None) -> None:
        message = f"iLink ret={ret}"
        if payload:
            message += f": {str(payload)[:200]}"
        super().__init__(message)
        self.message = message
        self.ret = ret
        self.payload = payload or {}

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
        self._base_url = _normalize_base_url(base_url)
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
        self._active_base_url = _normalize_base_url(base_url)
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
        _t0 = time.time()
        try:
            response = await self._client.post(url, json=body, headers=headers, timeout=req_timeout)
        except httpx.TimeoutException as e:
            _elapsed_ms = int((time.time() - _t0) * 1000)
            logger.error(
                "ilink.post.timeout url={} elapsed_ms={} error={}",
                url, _elapsed_ms, str(e)[:200],
            )
            raise
        except httpx.HTTPError as e:
            _elapsed_ms = int((time.time() - _t0) * 1000)
            logger.error(
                "ilink.post.http_error url={} elapsed_ms={} error={}",
                url, _elapsed_ms, str(e)[:200],
            )
            raise

        _elapsed_ms = int((time.time() - _t0) * 1000)
        if response.status_code != 200:
            logger.error(
                "ilink.post.bad_status url={} status={} elapsed_ms={} body={}",
                url, response.status_code, _elapsed_ms, response.text[:300],
            )
            raise RuntimeError(f"iLink HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except Exception as e:
            logger.error(
                "ilink.post.json_parse_failed url={} elapsed_ms={} error={} body={}",
                url, _elapsed_ms, str(e)[:200], response.text[:300],
            )
            raise RuntimeError(f"iLink 响应解析失败: {e}") from e

        # 检查 ret 码（部分接口无 ret 字段，跳过检查）
        ret = payload.get("ret")
        if ret is not None:
            if ret == RET_SESSION_EXPIRED:
                logger.warning(
                    "ilink.session_expired url={} elapsed_ms={}",
                    url, _elapsed_ms,
                )
                raise SessionExpiredError()
            if ret != RET_OK:
                logger.warning(
                    "ilink.post.bad_ret url={} ret={} elapsed_ms={} payload={}",
                    url, ret, _elapsed_ms, str(payload)[:300],
                )
                raise ILinkRetError(ret=ret, payload=payload)
        logger.debug("ilink.post.ok url={} ret={} elapsed_ms={}", url, ret, _elapsed_ms)
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
        _t0 = time.time()
        try:
            response = await self._client.get(
                url, params=params, headers=headers, timeout=req_timeout,
            )
        except httpx.TimeoutException as e:
            _elapsed_ms = int((time.time() - _t0) * 1000)
            logger.error(
                "ilink.get.timeout url={} elapsed_ms={} error={}",
                url, _elapsed_ms, str(e)[:200],
            )
            raise
        except httpx.HTTPError as e:
            _elapsed_ms = int((time.time() - _t0) * 1000)
            logger.error(
                "ilink.get.http_error url={} elapsed_ms={} error={}",
                url, _elapsed_ms, str(e)[:200],
            )
            raise

        _elapsed_ms = int((time.time() - _t0) * 1000)
        if response.status_code != 200:
            logger.error(
                "ilink.get.bad_status url={} status={} elapsed_ms={} body={}",
                url, response.status_code, _elapsed_ms, response.text[:300],
            )
            raise RuntimeError(f"iLink HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload = response.json()
        except Exception as e:
            logger.error(
                "ilink.get.json_parse_failed url={} elapsed_ms={} error={} body={}",
                url, _elapsed_ms, str(e)[:200], response.text[:300],
            )
            raise RuntimeError(f"iLink 响应解析失败: {e}") from e

        logger.debug(
            "ilink.get.ok url={} elapsed_ms={} payload_keys={}",
            url, _elapsed_ms, list(payload.keys()) if isinstance(payload, dict) else "n/a",
        )
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
                to_user_id[:16], len(text), ret,
            )
            return {"ret": ret}
        except ILinkRetError as e:
            # ret=-2 = 参数错误（context_token 过期/无效，或请求体不合规）
            # context_token 是消息路由必需字段，去掉它服务端无法路由，
            # 仍返回 ret=-2，故 tokenless 降级无效。
            # 恢复方式：等待该用户发新消息刷新 context_token 后再回复。
            # Minor#8（R3）：按 e.ret 精确判断，而非匹配异常字符串——
            # 否则 HTTP 400 响应体恰好含 "ret=-2" 文本时（如 JSON 原样透传）
            # 会误报为 context_token 过期（仅日志误导，行为一致）。
            if e.ret != -2:
                raise
            logger.warning(
                "ilink.send_message.ret_minus_2 to={} text_len={} "
                "ctx_token_len={} cause=context_token_expired_or_invalid "
                "recovery=user_must_send_new_message_to_refresh",
                to_user_id[:16], len(text), len(context_token),
            )
            raise

    @staticmethod
    def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB + PKCS7 加密（iLink 媒体上传协议）。"""
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    async def _get_upload_url(
        self, to_user_id: str, raw_bytes: bytes, aes_key: bytes
    ) -> tuple[str, str, str, bytes]:
        """获取 CDN 上传参数并预加密，返回 (upload_param, upload_full_url, filekey, encrypted)。

        Args:
            to_user_id: 目标用户 ID
            raw_bytes: 图片明文二进制
            aes_key: 随机 16 字节 AES 密钥

        Returns:
            (upload_param, upload_full_url, filekey, encrypted):
                上传参数、服务端返回的完整上传 URL（可为空）、文件标识、加密后数据。
                upload_full_url 优先于 upload_param 用于 CDN 上传（对齐官方 SDK）。
        """
        encrypted = self._aes_ecb_encrypt(raw_bytes, aes_key)
        filekey = os.urandom(16).hex()
        data = {
            "filekey": filekey,
            "media_type": 1,  # IMAGE
            "to_user_id": to_user_id,
            "rawsize": len(raw_bytes),
            "rawfilemd5": hashlib.md5(raw_bytes).hexdigest(),
            "filesize": len(encrypted),
            "no_need_thumb": True,  # 表情包无需缩略图
            "aeskey": aes_key.hex(),
        }
        payload = await self._post("/ilink/bot/getuploadurl", data=data)
        upload_param = payload.get("upload_param", "")
        upload_full_url = (payload.get("upload_full_url") or "").strip()
        logger.info(
            "ilink.get_upload_url.ok to={} upload_param_len={} full_url_len={}",
            to_user_id, len(upload_param), len(upload_full_url),
        )
        return upload_param, upload_full_url, filekey, encrypted

    async def _upload_to_cdn(
        self, upload_param: str, upload_full_url: str, filekey: str, encrypted: bytes
    ) -> str:
        """上传加密数据到 CDN，返回 x-encrypted-param（后续 sendmessage 引用）。

        优先使用服务端返回的 upload_full_url（官方 SDK 行为）；仅在服务端
        未返回时，才回退到 upload_param 拼接的固定 CDN URL。
        """
        if upload_full_url:
            url = upload_full_url
        else:
            query = urllib.parse.urlencode(
                {"encrypted_query_param": upload_param, "filekey": filekey}
            )
            url = f"{ILINK_CDN_URL}/upload?{query}"
        try:
            response = await self._client.post(
                url,
                content=encrypted,
                headers={"Content-Type": "application/octet-stream"},
            )
        except httpx.HTTPError as e:
            logger.error("ilink.cdn_upload.http_error url={} error={}", url, str(e)[:200])
            raise
        if response.status_code != 200:
            logger.error(
                "ilink.cdn_upload.bad_status status={} body={}",
                response.status_code, response.text[:200],
            )
            raise RuntimeError(
                f"iLink CDN upload HTTP {response.status_code}: {response.text[:120]}"
            )
        param = response.headers.get("x-encrypted-param", "")
        if not param:
            raise RuntimeError("iLink CDN upload missing x-encrypted-param")
        logger.info("ilink.cdn_upload.ok param_len={} used_full_url={}", len(param), bool(upload_full_url))
        return param

    async def send_media_message(
        self, to_user_id: str, context_token: str, text: str, image_path: str
    ) -> dict:
        """发送文字+图片消息（表情包）。

        完整流程：getuploadurl → AES-128-ECB 加密 → CDN 上传 →
        sendmessage 分两条独立消息发送（文本一条、图片一条）。

        对齐官方 @tencent-weixin/openclaw-weixin 的 sendMediaItems 实现：
        - 文本与图片分别作为独立请求发送，item_list 仅含单项。
          合并多条 item_list 会被服务端拒绝（ret=-2 invalid arguments）。
        - 出站 image_item 不含 aeskey 字段（该字段仅用于入站图片），
          只在 media.aes_key 携带 base64(hex字符串) 密钥。
        - media.aes_key 采用"hex 字符串的 ASCII 字节 base64"编码（官方
          Buffer.from(hex).toString('base64')），而非原始 16 字节 base64。

        Args:
            to_user_id: 接收方用户 ID
            context_token: 会话上下文 token
            text: 文本内容（可为空串，仅发图）
            image_path: 本地图片文件路径（表情包）

        Returns:
            字典包含:
                - ret (int): 图片消息返回码，0 表示成功

        Raises:
            FileNotFoundError: image_path 不存在
            SessionExpiredError: ret == -14
            httpx.HTTPError: 网络错误
            RuntimeError: 上传/发送失败（含 ret != 0）
        """
        # 治本修复（2026-08-05 用户"治标不治本"反馈）：同步文件读取 → asyncio.to_thread。
        # 根因：open(image_path, "rb").read() 是同步 IO，阻塞事件循环。
        #   sticker 发送期间事件循环被阻塞 → 并发的其他消息处理被卡住
        #   （用户连续发消息时第二条被第一条的 sticker IO 阻塞）。
        #   USB 盘上图片读取偶发慢（43KB 图片实测 100-500ms）。
        # asyncio.to_thread 在线程池执行文件读取，不阻塞事件循环。
        from pathlib import Path as _Path
        raw_bytes = await asyncio.to_thread(_Path(image_path).read_bytes)
        aes_key = os.urandom(16)
        upload_param, upload_full_url, filekey, encrypted = await self._get_upload_url(
            to_user_id, raw_bytes, aes_key
        )
        encrypted_param = await self._upload_to_cdn(
            upload_param, upload_full_url, filekey, encrypted
        )

        async def _send_single(item_list: list[dict]) -> int:
            """发送单条 item_list 消息，返回 ret 码。"""
            data = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": f"bot-{uuid.uuid4().hex[:16]}",
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": item_list,
                }
            }
            payload = await self._post("/ilink/bot/sendmessage", data=data)
            return payload.get("ret", RET_OK)

        # 文本与图片分开发送（官方 sendMediaItems 行为：item_list 仅含单项）
        if text:
            await _send_single([{"type": 1, "text_item": {"text": text}}])
        ret = await _send_single([
            {
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": encrypted_param,
                        # 官方格式：hex 字符串的 ASCII 字节 base64
                        # （等价 Buffer.from(hexstr).toString('base64')）
                        "aes_key": base64.b64encode(
                            aes_key.hex().encode("ascii")
                        ).decode("ascii"),
                        # 官方 SDK 必填字段，缺失会导致 sendmessage 返回
                        # ret=-2 "invalid arguments"（表情包发送失败根因）
                        "encrypt_type": 1,
                    },
                    "mid_size": len(encrypted),
                },
            }
        ])
        logger.info(
            "ilink.send_media_message.ok to={} text_len={} img_len={} ret={}",
            to_user_id[:16], len(text), len(raw_bytes), ret,
        )
        return {"ret": ret}

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
            user_id[:16], status, ret,
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
            user_id[:16], len(typing_ticket), ret,
        )
        return {
            "typing_ticket": typing_ticket,
            "ret": ret,
        }

    # ------------------------------------------------------------------
    # API: 测试消息（登录验证）
    # ------------------------------------------------------------------

    async def verify_token(self) -> tuple[bool, str]:
        """验证 bot_token 有效性（不发消息）。

        登录刚完成时还没有入站消息，没有 context_token，此时调 send_message
        会因 context_token 为空被服务端拒绝（ret=-2 prepare failed）。
        改用 getupdates 短超时(2s)探测 token：服务端认证通过会 hold 连接
        等消息（超时即代表认证通过）；认证失败会立即返回错误。

        Returns:
            tuple[bool, str]:
                - (True, "ok") token 有效（getupdates 返回 ret=0 或超时=hold）
                - (True, "session_expired") token 被识别但会话过期(-14)，
                  仍说明 token 本身有效，只是需重新登录
                - (False, error_msg) token 无效或网络错误
        """
        try:
            # Minor#1（R3）：探测游标用持久化游标起步而非空游标——空游标会从
            # 服务端最早积压回卷消费全部未确认消息（静默吞掉用户消息）；用上次
            # 持久化游标起步，把消费范围限制在"上次已确认位置之后"的增量，
            # 与 poller 恢复后的消费范围一致，最大程度缩小探测造成的数据丢失窗口。
            probe_cursor = self._load_probe_cursor()
            payload = await self._post(
                "/ilink/bot/getupdates",
                data={"get_updates_buf": probe_cursor},
                timeout=2.0,
            )
            # Q7 修复：探测会推进服务端消息游标（消费积压消息）——
            # 将返回的新游标持久化到 ~/.ai-agent/wechat_cursor.json（与
            # wechat_bot_adapter 同路径），供后续长轮询接续，避免消息被
            # 探测消费后丢失或按旧游标重放（重复处理）。
            next_cursor = payload.get("get_updates_buf", "") or ""
            if next_cursor:
                self._persist_verify_cursor(next_cursor)
            msgs = payload.get("msgs", []) or []
            if msgs:
                logger.warning(
                    "ilink.verify_token.consumed_pending_msgs count={} cursor_len={} "
                    "note=messages_consumed_by_probe_without_processing "
                    "hint=only_happens_when_no_active_poller",
                    len(msgs), len(next_cursor),
                )
            # ret=0：token 有效，且本次没有新消息
            logger.info("ilink.verify_token.ok")
            return True, "ok"
        except httpx.ReadTimeout:
            # 读超时 = 服务端 hold 连接等消息 = 认证通过（token 有效）。
            # 仅捕获 ReadTimeout：Connect/Write/PoolTimeout 不代表认证通过，
            # 它们可能是网络不可达/连接池耗尽，若误判为 ok 会掩盖真实故障。
            logger.info("ilink.verify_token.ok_via_timeout")
            return True, "ok"
        except SessionExpiredError:
            # -14：token 被服务端识别但会话过期（token 格式有效，需重新登录）
            logger.info("ilink.verify_token.session_expired")
            return True, "session_expired"
        except Exception as e:
            logger.warning(
                "ilink.verify_token.failed error={} type={}",
                str(e)[:200], type(e).__name__,
            )
            return False, f"{type(e).__name__}: {str(e)[:120]}"

    @staticmethod
    def _load_probe_cursor() -> str:
        """读取已持久化的探测/轮询游标（无则返回空串）。

        与 wechat_bot_adapter._cursor_path 路径保持一致（凭证同目录）。
        供 verify_token 探测起步用，避免空游标从服务端最早积压回卷消费。
        """
        try:
            import json as _json
            from pathlib import Path as _Path

            cursor_path = _Path.home() / ".ai-agent" / "wechat_cursor.json"
            if cursor_path.exists():
                data = _json.loads(cursor_path.read_text(encoding="utf-8"))
                cursor = data.get("cursor", "") or ""
                if cursor:
                    logger.info("ilink.verify_probe_cursor_loaded len={}", len(cursor))
                    return cursor
        except Exception as e:
            logger.warning(
                "ilink.verify_probe_cursor_load_failed error={}",
                str(e)[:120],
            )
        return ""

    @staticmethod
    def _persist_verify_cursor(cursor: str) -> None:
        """持久化 verify_token 探测后推进的服务端游标。

        探测用空游标 getupdates，服务端会把这些消息标记已投递并推进游标；
        若不持久化，后续轮询按旧游标拉取会重放历史消息（重复处理）或丢消息。
        路径与 wechat_bot_adapter._cursor_path 保持一致（凭证同目录）。
        """
        if not cursor:
            return
        try:
            import json as _json
            import os as _os
            from pathlib import Path as _Path

            cursor_path = _Path.home() / ".ai-agent" / "wechat_cursor.json"
            cursor_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = cursor_path.with_suffix(".tmp")
            tmp.write_text(
                _json.dumps({"cursor": cursor}, ensure_ascii=False),
                encoding="utf-8",
            )
            # Minor#4（R3）：游标含会话状态信息，权限对齐凭证文件（0600），
            # 避免 umask 默认权限（如 0644）导致同机其他用户可读。
            _os.chmod(tmp, 0o600)
            tmp.replace(cursor_path)
            logger.info("ilink.verify_cursor_persisted len={}", len(cursor))
        except Exception as e:
            logger.warning(
                "ilink.verify_cursor_persist_failed error={}",
                str(e)[:120],
            )

    async def send_test_message(self, bot_token: str, user_id: str) -> tuple[bool, str]:
        """验证登录是否成功（通过 token 探测，不发消息）。

        登录刚完成时无 context_token，发消息会 ret=-2（prepare failed）。
        改为调用 verify_token 用 getupdates 短超时探测 bot_token 有效性。

        Args:
            bot_token: 待验证的 bot_token（兼容签名，实际用 self._bot_token）
            user_id: 测试用户 ID（兼容签名，探测不依赖此字段）

        Returns:
            tuple[bool, str]:
                - (True, "ok") token 有效
                - (False, error_msg) token 无效或网络错误

        Note:
            不抛异常，所有错误经返回值传递，方便登录流程调用。
        """
        logger.info("ilink.send_test_message.start user={} token_len={}", user_id, len(bot_token))
        return await self.verify_token()


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
