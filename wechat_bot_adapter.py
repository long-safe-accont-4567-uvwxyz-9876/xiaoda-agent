"""微信 Bot 适配器（iLink 协议）

基于 ilink_client.py 实现微信官方 Bot API 适配器，
复用 AgentCore 处理消息，结构对齐 qq_bot_adapter.py。

iLink 协议：微信官方 Bot API，域名 ilinkai.weixin.qq.com，HTTP/JSON。
- 凭证持久化（~/.ai-agent/wechat_credentials.json）
- 长轮询收消息（get_updates，服务器挂起 35 秒）
- 调用 AgentCore.process() 处理文本消息
- 通过 send_message 回复
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ilink_client import ILinkClient, SessionExpiredError


# ============================================================================
# 常量定义
# ============================================================================

# 凭证文件路径：与 config.py 的 ~/.ai-agent/ 目录约定一致
CREDENTIALS_PATH = Path.home() / ".ai-agent" / "wechat_credentials.json"

# iLink 默认服务端地址（登录后可能被 baseurl 覆盖）
ILINK_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

# 当前活跃的 bot 实例（对齐 qq_bot_adapter 的 _ACTIVE_BOT 模式，
# 供同进程内主动消息入口使用）
_ACTIVE_BOT: "WeChatBotAdapter | None" = None


class WeChatBotAdapter:
    """微信 Bot 适配器（iLink 协议）

    基于 ILinkClient 实现微信官方 Bot API：
    - 凭证持久化（~/.ai-agent/wechat_credentials.json）
    - 长轮询收消息（get_updates）
    - 调用 AgentCore.process() 处理文本消息
    - 通过 send_message 回复

    结构对齐 qq_bot_adapter.py，复用全部 emotion/nudge/memory/RAG 模块。
    """

    def __init__(
        self,
        db: Any,
        router: Any,
        api: Any,
        user_openid: str,
        core: Any = None,
        config_service: Any = None,
        portrait_manager: Any = None,
    ) -> None:
        """初始化微信 Bot 适配器

        Args:
            db: 数据库实例
            router: 模型路由器
            api: iLink API 客户端（兼容旧接口，适配器内部自建 ILinkClient）
            user_openid: 用户微信 openid
            core: AgentCore 实例（未提供时由 start() 自建）
            config_service: 配置服务
            portrait_manager: 用户画像管理器
        """
        self._db = db
        self._router = router
        self._api = api
        self._core = core
        self._user_openid = user_openid
        self._config_service = config_service
        self._portrait_manager = portrait_manager

        # iLink 客户端（start 时按凭证初始化）
        self._ilink_client: Optional[ILinkClient] = None

        # 运行状态
        self._running = False
        self._closed = False
        self._connected = False
        self._expired = False
        self._poll_task: Optional[asyncio.Task] = None

        # 长轮询游标（首次为空字符串，后续传入上次返回的 get_updates_buf）
        self._cursor: str = ""

        # 最近一条消息的上下文（send_message 回复时使用）
        self._last_context_token: str = ""
        self._last_from_user_id: str = ""

        logger.info(
            "wechat_bot.init user={}",
            user_openid[:8] if user_openid else "unknown",
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动微信 Bot 适配器

        - 检测凭证文件 ~/.ai-agent/wechat_credentials.json
        - 有凭证：用凭证初始化 ILinkClient，直接启动消息轮询
        - 无凭证：等待 WebUI 触发扫码流程（不自动启动轮询）
        - 设置 _ACTIVE_BOT = self（对齐 qq_bot_adapter 的模式）
        """
        global _ACTIVE_BOT
        # guard：停掉已有活跃实例，避免多个 poll_task 并存导致同一消息被多次
        # 接收/处理（"消息重复 N 次"根因：旧实例的 _poll_task 不会被新实例自动取消，
        # _start_polling 的幂等只对同一实例生效，跨实例无效）
        if _ACTIVE_BOT is not None and _ACTIVE_BOT is not self and not _ACTIVE_BOT.is_closed():
            logger.info("wechat_bot.start_stopping_existing")
            try:
                await _ACTIVE_BOT.stop()
            except Exception as e:
                logger.warning(
                    "wechat_bot.start_stop_existing_failed error={}", str(e)[:200],
                )
        _ACTIVE_BOT = self
        self._running = True
        self._closed = False

        # 确保 AgentCore 已初始化（未注入时尝试自建）
        if self._core is None:
            try:
                from agent_core import AgentCore
                self._core = AgentCore()
                logger.info("wechat_bot.agent_core_created")
            except Exception as e:
                logger.error(
                    "wechat_bot.agent_core_create_failed error={}",
                    str(e)[:200],
                )

        # 初始化 AgentCore（如尚未初始化）
        if self._core is not None and not getattr(self._core, "_initialized", False):
            try:
                await self._core.init()
                logger.info("wechat_bot.agent_core_initialized")
            except Exception as e:
                # 不重抛，避免阻断适配器启动（与 qq_bot_adapter.on_ready 策略一致）
                logger.error(
                    "wechat_bot.agent_core_init_failed error={}",
                    str(e)[:200],
                    exc_info=True,
                )

        # 加载凭证
        creds = self._load_credentials()
        if creds:
            # 有凭证：直接初始化 ILinkClient 并启动轮询
            try:
                self._ilink_client = ILinkClient(
                    base_url=creds.get("baseurl") or ILINK_DEFAULT_BASE_URL,
                    bot_token=creds.get("bot_token", ""),
                )
                self._connected = True
                self._expired = False
                logger.info("wechat_bot.credentials_loaded starting_poller")
                self._start_polling()
            except Exception as e:
                logger.error(
                    "wechat_bot.ilink_init_failed error={}",
                    str(e)[:200],
                )
                self._connected = False
        else:
            # 无凭证：等待 WebUI 触发扫码流程（不自动启动轮询）
            logger.info("wechat_bot.no_credentials waiting_for_scan")

    def _start_polling(self) -> None:
        """启动消息轮询任务（幂等：已运行时不重复创建）。"""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_messages())

    async def stop(self) -> None:
        """停止微信 Bot 适配器

        - 取消轮询任务
        - 清理状态
        - 设置 _ACTIVE_BOT = None
        """
        global _ACTIVE_BOT
        self._running = False
        self._closed = True
        self._connected = False

        # 取消轮询任务
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(
                    "wechat_bot.poll_task_cancel_error error={}",
                    str(e)[:200],
                )
            self._poll_task = None

        # 关闭 ILinkClient
        if self._ilink_client is not None:
            try:
                await self._ilink_client.close()
            except Exception as e:
                logger.warning(
                    "wechat_bot.ilink_close_error error={}",
                    str(e)[:200],
                )
            self._ilink_client = None

        if _ACTIVE_BOT is self:
            _ACTIVE_BOT = None

        logger.info("wechat_bot.stopped")

    # ------------------------------------------------------------------
    # 长轮询
    # ------------------------------------------------------------------

    async def _poll_messages(self) -> None:
        """长轮询循环：调用 get_updates 收消息并分发处理

        - 调用 ilink_client.get_updates(cursor) 收消息
        - 处理 SessionExpiredError（ret: -14）：清除凭证，设置 _expired=True，停止轮询
        - 每条消息调用 _process_message(msg) 处理（用 asyncio.create_task 避免阻塞轮询）
        - 更新 cursor（get_updates_buf）
        """
        if self._ilink_client is None:
            logger.warning("wechat_bot.poll_no_client")
            return

        logger.info("wechat_bot.poll_started")
        while self._running and not self._expired:
            try:
                result = await self._ilink_client.get_updates(self._cursor)
            except SessionExpiredError:
                # 会话过期：清除凭证，标记过期，停止轮询
                logger.warning("wechat_bot.session_expired clearing_credentials")
                self._expired = True
                self._connected = False
                self._clear_credentials()
                break
            except asyncio.CancelledError:
                logger.info("wechat_bot.poll_cancelled")
                raise
            except Exception as e:
                # 网络错误等：退避后重试
                logger.error(
                    "wechat_bot.poll_error error={} retry_in=5s",
                    str(e)[:200],
                )
                await asyncio.sleep(5)
                continue

            # 更新游标（get_updates_buf）
            self._cursor = result.get("cursor", "") or ""

            # 更新上下文 token（用于后续 send_message）
            ctx_token = result.get("context_token", "") or ""
            if ctx_token:
                self._last_context_token = ctx_token

            # 分发消息（用 asyncio.create_task 避免阻塞轮询）
            msgs = result.get("msgs", []) or []
            for msg in msgs:
                asyncio.create_task(self._process_message(msg))

        logger.info(
            "wechat_bot.poll_stopped expired={} running={}",
            self._expired,
            self._running,
        )

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    async def _process_message(self, msg: dict) -> None:
        """处理收到的微信消息

        - 解析消息类型（item_list[].type：1=文本, 2=图片, 3=语音, 4=文件, 5=视频）
        - 文本消息：提取 text_item.text，调用 self._core.process() 处理
        - 缓存 context_token 和 from_user_id（回复时需要）
        - Agent 回复通过 send_message() 发回微信
        - 其他类型消息暂不处理，记录日志

        Args:
            msg: get_updates 返回的消息字典，包含 from_user_id/context_token/item_list 等字段
        """
        if not isinstance(msg, dict):
            logger.warning("wechat_bot.msg_not_dict msg={}", str(msg)[:200])
            return

        from_user_id = msg.get("from_user_id", "") or ""
        context_token = msg.get("context_token", "") or ""

        # 缓存上下文（send_message 回复时使用）
        if from_user_id:
            self._last_from_user_id = from_user_id
        if context_token:
            self._last_context_token = context_token

        item_list = msg.get("item_list", []) or []
        if not item_list:
            logger.info("wechat_bot.empty_item_list from_user={}", from_user_id[:16])
            return

        for item in item_list:
            item_type = item.get("type", 0)
            if item_type == 1:
                # 文本消息：提取 text_item.text
                text_item = item.get("text_item", {}) or {}
                text = text_item.get("text", "").strip()
                if not text:
                    continue
                await self._handle_text_message(text, from_user_id, context_token)
            elif item_type == 2:
                logger.info("wechat_bot.image_msg_skipped from_user={}", from_user_id[:16])
            elif item_type == 3:
                logger.info("wechat_bot.voice_msg_skipped from_user={}", from_user_id[:16])
            elif item_type == 4:
                logger.info("wechat_bot.file_msg_skipped from_user={}", from_user_id[:16])
            elif item_type == 5:
                logger.info("wechat_bot.video_msg_skipped from_user={}", from_user_id[:16])
            else:
                logger.info(
                    "wechat_bot.unknown_msg_type type={} from_user={}",
                    item_type,
                    from_user_id[:16],
                )

    async def _handle_text_message(
        self, text: str, from_user_id: str, context_token: str
    ) -> None:
        """处理文本消息：调用 AgentCore.process() 并回复

        Args:
            text: 用户消息文本
            from_user_id: 发送方用户 ID（回复时作为 to_user_id）
            context_token: 会话上下文 token
        """
        if self._core is None:
            logger.warning("wechat_bot.no_core text={}", text[:80])
            return

        user_id = f"wechat_{from_user_id}" if from_user_id else "wechat_unknown"
        logger.info("wechat_bot.text_msg user_id={} text={}", user_id, text[:80])

        # ACK：处理前立即发送"收到啦，正在想"（对齐 QQ 行为）
        try:
            from emotion.emoji_config import get_ack_message
            ack_text = get_ack_message("xiaoda")
            await self.send_message(
                ack_text,
                to_user_id=from_user_id,
                context_token=context_token,
            )
        except Exception as e:
            logger.warning("wechat_bot.ack_send_failed error={}", str(e)[:200])

        try:
            result = await asyncio.wait_for(
                self._core.process(
                    text,
                    user_id=user_id,
                    source="wechat_c2c",
                    user_openid=from_user_id,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.warning("wechat_bot.process_timeout user_id={}", user_id)
            await self.send_message(
                "处理超时，请稍后再试",
                to_user_id=from_user_id,
                context_token=context_token,
            )
            return
        except Exception as e:
            logger.error(
                "wechat_bot.process_error user_id={} error={}",
                user_id,
                str(e)[:200],
                exc_info=True,
            )
            await self.send_message(
                "出了点小问题，等会儿再聊好不好？",
                to_user_id=from_user_id,
                context_token=context_token,
            )
            return

        reply = getattr(result, "reply", "") or ""
        if reply:
            await self.send_message(
                reply,
                to_user_id=from_user_id,
                context_token=context_token,
            )

    # ------------------------------------------------------------------
    # 发送消息
    # ------------------------------------------------------------------

    async def send_message(
        self,
        content: str,
        msg_type: str = "text",
        to_user_id: str = "",
        context_token: str = "",
    ) -> bool:
        """发送消息给微信用户

        调用 ilink_client.send_message(to_user_id, context_token, text)
        回传缓存的 context_token

        Args:
            content: 消息内容
            msg_type: 消息类型（目前仅支持 text）
            to_user_id: 接收方用户 ID（为空时使用缓存的 _last_from_user_id）
            context_token: 会话上下文 token（为空时使用缓存的 _last_context_token）

        Returns:
            是否发送成功
        """
        if self._ilink_client is None:
            logger.warning("wechat_bot.send_no_client content={}", content[:80])
            return False

        target_user = to_user_id or self._last_from_user_id
        target_token = context_token or self._last_context_token
        if not target_user:
            logger.warning("wechat_bot.send_no_user_id content={}", content[:80])
            return False

        try:
            result = await self._ilink_client.send_message(
                target_user, target_token, content
            )
            ret = result.get("ret", 0)
            return ret == 0
        except SessionExpiredError:
            logger.warning("wechat_bot.send_session_expired")
            self._expired = True
            self._connected = False
            self._clear_credentials()
            return False
        except Exception as e:
            # ret=-2: context_token 过期/无效。AgentCore 处理最长 120s，
            # 期间用户若发了新消息，_last_context_token 已更新为最新，
            # 用它重试一次可能恢复投递（覆盖"处理慢导致 token 过期"场景）。
            if (
                "ret=-2" in str(e)
                and self._last_context_token
                and self._last_context_token != target_token
            ):
                logger.info(
                    "wechat_bot.send_retry_with_cached_token user={} "
                    "stale_token_len={} cached_token_len={}",
                    target_user[:16], len(target_token), len(self._last_context_token),
                )
                try:
                    result = await self._ilink_client.send_message(
                        target_user, self._last_context_token, content
                    )
                    ret = result.get("ret", 0)
                    if ret == 0:
                        logger.info("wechat_bot.send_retry_ok user={}", target_user[:16])
                    return ret == 0
                except Exception as e2:
                    logger.error(
                        "wechat_bot.send_retry_failed user={} error={}",
                        target_user[:16], str(e2)[:200],
                    )
                    return False
            logger.error("wechat_bot.send_error error={}", str(e)[:200])
            return False

    async def send_sticker(self, sticker_path: str) -> bool:
        """发送微信表情包（暂未实现）

        TODO: 上传图片到微信 CDN → 发送图片消息

        Args:
            sticker_path: 表情包文件路径

        Returns:
            是否发送成功
        """
        logger.debug("wechat_bot.send_sticker_not_implemented path={}", sticker_path)
        return False

    async def send_voice(self, audio_path: str) -> bool:
        """发送微信语音消息（暂未实现）

        TODO: 上传语音到微信 CDN → 发送语音消息

        Args:
            audio_path: 语音文件路径

        Returns:
            是否发送成功
        """
        logger.debug("wechat_bot.send_voice_not_implemented path={}", audio_path)
        return False

    # ------------------------------------------------------------------
    # 凭证持久化
    # ------------------------------------------------------------------

    def _save_credentials(
        self,
        bot_token: str,
        ilink_bot_id: str,
        ilink_user_id: str,
        baseurl: str,
    ) -> None:
        """保存凭证到 ~/.ai-agent/wechat_credentials.json

        Args:
            bot_token: iLink Bearer token
            ilink_bot_id: 登录后的 bot ID
            ilink_user_id: 登录后的 user ID
            baseurl: 服务端下发的活跃 base URL
        """
        try:
            CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "bot_token": bot_token,
                "ilink_bot_id": ilink_bot_id,
                "ilink_user_id": ilink_user_id,
                "baseurl": baseurl,
            }
            CREDENTIALS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("wechat_bot.credentials_saved path={}", CREDENTIALS_PATH)
        except Exception as e:
            logger.error(
                "wechat_bot.credentials_save_failed error={}",
                str(e)[:200],
            )

    def _load_credentials(self) -> Optional[dict]:
        """从 ~/.ai-agent/wechat_credentials.json 加载凭证

        Returns:
            凭证字典（含 bot_token/ilink_bot_id/ilink_user_id/baseurl），
            文件不存在或无效时返回 None
        """
        if not CREDENTIALS_PATH.exists():
            return None
        try:
            data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            bot_token = data.get("bot_token", "")
            if not bot_token:
                return None
            return data
        except Exception as e:
            logger.error(
                "wechat_bot.credentials_load_failed error={}",
                str(e)[:200],
            )
            return None

    def _clear_credentials(self) -> None:
        """删除凭证文件"""
        try:
            if CREDENTIALS_PATH.exists():
                CREDENTIALS_PATH.unlink()
                logger.info("wechat_bot.credentials_cleared path={}", CREDENTIALS_PATH)
        except Exception as e:
            logger.warning(
                "wechat_bot.credentials_clear_failed error={}",
                str(e)[:200],
            )

    # ------------------------------------------------------------------
    # 状态属性
    # ------------------------------------------------------------------

    def is_closed(self) -> bool:
        """返回是否已停止"""
        return self._closed


# ============================================================================
# 工厂函数
# ============================================================================


def create_wechat_bot(
    db: Any,
    router: Any,
    api: Any = None,
    user_openid: str = "",
    core: Any = None,
    config_service: Any = None,
    portrait_manager: Any = None,
) -> WeChatBotAdapter:
    """创建微信 Bot 适配器实例

    配置项：
    - WECHAT_ILINK_ENABLED: 是否启用微信桥接（默认 false）

    Args:
        db: 数据库实例
        router: 模型路由器
        api: iLink API 客户端（兼容旧接口，适配器内部自建 ILinkClient）
        user_openid: 用户微信 openid
        core: AgentCore 实例（未提供时由 start() 自建）
        config_service: 配置服务
        portrait_manager: 用户画像管理器

    Returns:
        WeChatBotAdapter 实例
    """
    enabled = os.getenv("WECHAT_ILINK_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        logger.info(
            "wechat_bot.skeleton_mode set WECHAT_ILINK_ENABLED=true to enable"
        )
    return WeChatBotAdapter(
        db=db,
        router=router,
        api=api,
        user_openid=user_openid,
        core=core,
        config_service=config_service,
        portrait_manager=portrait_manager,
    )
