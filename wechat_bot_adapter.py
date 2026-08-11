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
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ilink_client import ILinkClient, SessionExpiredError, ILinkRetError


# ============================================================================
# 常量定义
# ============================================================================

# 凭证文件路径：与 config.py 的 ~/.ai-agent/ 目录约定一致
CREDENTIALS_PATH = Path.home() / ".ai-agent" / "wechat_credentials.json"


# ============================================================================
# 模块级凭证操作（供路由层直接调用，无需创建 adapter 实例）
# ============================================================================

def save_credentials(bot_token: str, ilink_bot_id: str, ilink_user_id: str, baseurl: str) -> None:
    """保存凭证到 ~/.ai-agent/wechat_credentials.json

    模块级函数，供路由层直接调用，无需创建 WeChatBotAdapter 实例。

    Args:
        bot_token: iLink Bearer token
        ilink_bot_id: 登录后的 bot ID
        ilink_user_id: 登录后的 user ID
        baseurl: 服务端下发的活跃 base URL
    """
    try:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "bot_token": bot_token,
            "ilink_bot_id": ilink_bot_id,
            "ilink_user_id": ilink_user_id,
            "baseurl": baseurl,
        }
        # 原子写入 + 限制权限：先写临时文件 chmod 0600，再 replace 覆盖，
        # 避免明文 token 权限过宽、且失败时留下半截文件。
        tmp_path = CREDENTIALS_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(CREDENTIALS_PATH)
        logger.info("wechat_bot.credentials_saved path={}", CREDENTIALS_PATH)
    except Exception as e:
        logger.error(
            "wechat_bot.credentials_save_failed error={}",
            str(e)[:200],
        )
        raise


def load_credentials() -> Optional[dict]:
    """从 ~/.ai-agent/wechat_credentials.json 加载凭证

    模块级函数，供路由层直接调用，无需创建 WeChatBotAdapter 实例。

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


def clear_credentials() -> None:
    """删除凭证文件

    模块级函数，供路由层直接调用，无需创建 WeChatBotAdapter 实例。
    """
    try:
        if CREDENTIALS_PATH.exists():
            CREDENTIALS_PATH.unlink()
            logger.info("wechat_bot.credentials_cleared path={}", CREDENTIALS_PATH)
    except Exception as e:
        logger.warning(
            "wechat_bot.credentials_clear_failed error={}",
            str(e)[:200],
        )

# iLink 默认服务端地址（登录后可能被 baseurl 覆盖）
ILINK_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

# 当前活跃的 bot 实例（对齐 qq_bot_adapter 的 _ACTIVE_BOT 模式，
# 供同进程内主动消息入口使用）
_ACTIVE_BOT: "WeChatBotAdapter | None" = None


async def send_proactive_message(text: str) -> bool:
    """向最近微信私聊用户主动发一条消息（供 web/greeting_scheduler 等调用）。

    微信 iLink 协议要求 context_token 才能路由消息，该 token 只能在
    bot 轮询收到用户消息时缓存。因此主动发送依赖活跃 bot 实例
    （_ACTIVE_BOT 已缓存最近 _last_from_user_id / _last_context_token）。

    Raises:
        RuntimeError: bot 未启动 / 尚未收到过用户消息（无 context_token）
    """
    bot = _ACTIVE_BOT
    if bot is None or bot.is_closed():
        raise RuntimeError("微信 client 未连接（请先在 WebUI 扫码登录并启动微信 Bot）")
    if not bot._last_from_user_id:
        raise RuntimeError(
            "还没有可用的微信用户上下文（等用户先在微信上发一条消息，Bot 收到后才能主动回复）"
        )
    target_token = bot._ctx_by_user.get(bot._last_from_user_id, "")
    if not target_token:
        raise RuntimeError(
            "还没有可用的微信用户上下文（等用户先在微信上发一条消息，Bot 收到后才能主动回复）"
        )
    ok = await bot.send_message(text)
    if not ok:
        raise RuntimeError("微信消息发送失败（ret != 0）")
    logger.info("wechat_bot.proactive_sent to={} text={}", bot._last_from_user_id[:16], text[:40])
    return True

# 串行化 start() 中活跃实例的 check→stop→assign 过渡，
# 避免并发 start() 交错产生多个 poller 或覆盖未停止的旧实例。
_START_LOCK: "asyncio.Lock" = asyncio.Lock()


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
        self._init_failed = False  # W7：AgentCore.init() 失败状态，供 /wechat/status 暴露
        self._poll_task: Optional[asyncio.Task] = None

        # 消息处理任务集合：持有强引用避免被 GC 回收，stop() 时统一取消
        self._msg_tasks: set[asyncio.Task] = set()

        # 长轮询游标（首次为空字符串，后续传入上次返回的 get_updates_buf）
        # 持久化到凭证同目录：进程重启后恢复游标，避免服务端重放历史消息。
        self._cursor: str = self._load_cursor()

        # 最近一条消息的上下文（send_message 回复时使用）
        # W1 修复：改为 per-user 映射，避免多用户并发覆盖导致串话/发错人。
        self._ctx_by_user: dict[str, str] = {}
        self._last_from_user_id: str = ""

        # 消息去重缓存：msg_id → 时间戳，保留最近 1 小时（对齐 qq_bot_adapter）。
        # 仅做 msg_id 级去重（同一 msg_id 的精确重复才拦截），不做内容级去重。
        self._processed_msg_ids: dict[str, float] = {}
        self._MSG_ID_TTL = 3600  # 1 小时

        # W3：per-user 串行锁——同一用户消息串行处理，不同用户并发（对齐 qq_bot_adapter）。
        # 防止同用户连发消息时并发调用 AgentCore 导致会话上下文竞争、回复乱序。
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._user_locks_ts: dict[str, float] = {}
        self._USER_LOCK_TTL = 3600  # 锁缓存 1 小时，超时清理避免长期运行内存增长

        # W5：会话过期（ret=-14）自动恢复——服务端瞬时状态时指数退避重试，
        # 连续多次失败才判定 token 真正失效。避免一次抖动就清凭证强制人工重扫码。
        self._expire_retries = 0
        self._MAX_EXPIRE_RETRIES = 3

        logger.info(
            "wechat_bot.init user={}",
            user_openid[:8] if user_openid else "unknown",
        )

    # ------------------------------------------------------------------
    # 消息去重与游标持久化
    # ------------------------------------------------------------------

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        """msg_id 级去重：同一 msg_id 的精确重复才拦截（对齐 qq_bot_adapter）。

        网络抖动重试/进程重启后服务端重放时，防止同一消息被处理多次
        （每次处理都会发 ACK + 回复，token 成本翻倍）。
        """
        now = time.time()
        expired = [k for k, ts in self._processed_msg_ids.items() if now - ts > self._MSG_ID_TTL]
        for k in expired:
            del self._processed_msg_ids[k]
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids[msg_id] = now
        return False

    @staticmethod
    def _cursor_path() -> Path:
        """游标持久化文件路径（与凭证同目录，复用 0600 目录）。"""
        return CREDENTIALS_PATH.with_name("wechat_cursor.json")

    def _load_cursor(self) -> str:
        try:
            path = self._cursor_path()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                cursor = data.get("cursor", "") or ""
                if cursor:
                    logger.info("wechat_bot.cursor_loaded len={}", len(cursor))
                    return cursor
        except Exception as e:
            logger.warning("wechat_bot.cursor_load_failed error={}", str(e)[:120])
        return ""

    def _save_cursor(self) -> None:
        if not self._cursor:
            return
        try:
            path = self._cursor_path()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"cursor": self._cursor}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            logger.warning("wechat_bot.cursor_save_failed error={}", str(e)[:120])

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
        # 串行化活跃实例的 check→stop→assign 过渡：并发 start() 交错会产生
        # 多个 poll_task 或覆盖未停止的旧实例（"消息重复 N 次"根因之一）。
        # W2 修复：_running/_closed 状态赋值必须在锁内完成——
        # 否则并发 start 时旧实例被 stop 后，锁外代码会把 _closed 覆盖回 False，
        # 继续拉起来第二个 poller（同凭证同游标 → 同条消息双份 ACK + 双份回复）。
        async with _START_LOCK:
            # 幂等：同一实例已在运行则直接返回，避免重复建 poller / 覆盖 client
            if _ACTIVE_BOT is self and self._running and not self._closed:
                logger.info("wechat_bot.start_already_running")
                return
            if _ACTIVE_BOT is not None and _ACTIVE_BOT is not self and not _ACTIVE_BOT.is_closed():
                logger.info("wechat_bot.start_stopping_existing")
                try:
                    await _ACTIVE_BOT.stop()
                except Exception as e:
                    logger.warning(
                        "wechat_bot.start_stop_existing_failed error={}",
                        str(e)[:200],
                    )
                    # 旧实例未完全停止：不覆盖活跃引用，避免新旧 poller 并存
                    raise RuntimeError(
                        "failed to stop previous wechat bot instance"
                    ) from e
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
                # W7 修复：记录失败状态并在 /wechat/status 暴露，故障可见。
                # 不重抛，避免阻断适配器启动（与 qq_bot_adapter.on_ready 策略一致）
                logger.error(
                    "wechat_bot.agent_core_init_failed error={}",
                    str(e)[:200],
                    exc_info=True,
                )
                self._init_failed = True

        # W2：并发 stop() 可能在锁外 await 期间被调用（新实例接管），
        # 若已被停止则中断启动，绝不再拉起 poller。
        if self._closed:
            logger.info("wechat_bot.start_aborted_stopped_concurrently")
            return

        # 加载凭证
        creds = self._load_credentials()
        if creds:
            # 有凭证：直接初始化 ILinkClient 并启动轮询
            try:
                baseurl = creds.get("baseurl") or ""
                if baseurl and not baseurl.lower().startswith("https://"):
                    # 拒绝非 HTTPS 地址，避免 Bearer token 明文传输
                    logger.warning(
                        "wechat_bot.rejected_non_https_baseurl url={}",
                        baseurl[:200],
                    )
                    baseurl = ""
                self._ilink_client = ILinkClient(
                    base_url=baseurl or ILINK_DEFAULT_BASE_URL,
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

        # 取消未完成的消息处理任务（best-effort，避免断开后仍跑 AgentCore 至 120s）
        if self._msg_tasks:
            for task in list(self._msg_tasks):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*self._msg_tasks, return_exceptions=True)
            except Exception as e:
                logger.warning(
                    "wechat_bot.msg_task_cancel_error error={}",
                    str(e)[:200],
                )
            self._msg_tasks.clear()

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
        - 错误重试使用指数退避（1s→2s→4s→8s，最大30s）
        """
        if self._ilink_client is None:
            logger.warning("wechat_bot.poll_no_client")
            return

        logger.info("wechat_bot.poll_started cursor_len={}", len(self._cursor))
        _backoff = 1.0  # 初始退避时间 1 秒
        _max_backoff = 30.0  # 最大退避时间
        _poll_count = 0
        while self._running and not self._expired:
            _poll_count += 1
            _t0 = time.time()
            try:
                result = await self._ilink_client.get_updates(self._cursor)
                _elapsed_ms = int((time.time() - _t0) * 1000)
                # 成功轮询：重置退避与会话过期计数
                if _backoff > 1.0:
                    logger.info(
                        "wechat_bot.poll_recovered after_backoff={:.0f}s",
                        _backoff,
                    )
                    _backoff = 1.0
                if self._expire_retries:
                    logger.info(
                        "wechat_bot.session_expired_recovered retries={}",
                        self._expire_retries,
                    )
                    self._expire_retries = 0
                self._expired = False
                self._connected = True
            except SessionExpiredError:
                # 会话过期（ret=-14）：token 可能已失效，但也可能是服务端瞬时状态。
                # W5：不立即清凭证——指数退避重试（5s→10s→20s→…封顶60s），
                # 连续 MAX_EXPIRE_RETRIES 次失败才判定真正过期：清凭证、停止轮询、
                # 等待 WebUI 重新扫码。瞬时抖动时自动恢复，避免 bot 无故掉线。
                self._expire_retries += 1
                self._connected = False
                if self._expire_retries >= self._MAX_EXPIRE_RETRIES:
                    logger.error(
                        "wechat_bot.session_expired_confirmed retries={} "
                        "clearing_credentials",
                        self._expire_retries,
                    )
                    self._expired = True
                    self._clear_credentials()
                    break
                delay = min(
                    5 * (2 ** (self._expire_retries - 1)),
                    60.0,
                )
                logger.warning(
                    "wechat_bot.session_expired_retry retry={} retry_in={:.0f}s",
                    self._expire_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except asyncio.CancelledError:
                logger.info("wechat_bot.poll_cancelled")
                raise
            except Exception as e:
                # 网络错误等：指数退避后重试
                logger.error(
                    "wechat_bot.poll_error error={} retry_in={:.0f}s poll_count={}",
                    str(e)[:200], _backoff, _poll_count,
                )
                await asyncio.sleep(_backoff)
                # 指数退避：1→2→4→8→16→30（封顶）
                _backoff = min(_backoff * 2, _max_backoff)
                continue

            # 更新游标（get_updates_buf）：为空时保留原游标，避免重放历史消息。
            # W1：持久化游标，进程重启后恢复，避免服务端按空游标重放历史消息。
            next_cursor = result.get("cursor", "") or ""
            if next_cursor:
                self._cursor = next_cursor
                self._save_cursor()

            # 更新上下文 token（用于后续 send_message）——绑定最近用户
            ctx_token = result.get("context_token", "") or ""
            if ctx_token and self._last_from_user_id:
                self._ctx_by_user[self._last_from_user_id] = ctx_token

            # 分发消息（用 asyncio.create_task 避免阻塞轮询）
            msgs = result.get("msgs", []) or []
            if msgs:
                logger.info(
                    "wechat_bot.poll_received poll_count={} elapsed_ms={} msg_count={} cursor_len={}",
                    _poll_count, _elapsed_ms, len(msgs), len(next_cursor),
                )
                for msg in msgs:
                    task = asyncio.create_task(self._process_message(msg))
                    self._msg_tasks.add(task)
                    task.add_done_callback(self._on_msg_task_done)
            else:
                # 无消息时短暂 sleep，避免紧密循环
                await asyncio.sleep(0.5)

        logger.info(
            "wechat_bot.poll_stopped expired={} running={} total_polls={}",
            self._expired,
            self._running,
            _poll_count,
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
        msg_id = msg.get("msg_id", "") or ""

        logger.info(
            "wechat_bot.msg_received from_user={} msg_id={} ctx_token_len={} item_count={}",
            from_user_id[:16], msg_id[:12], len(context_token),
            len(msg.get("item_list", []) or []),
        )

        # W1：msg_id 级去重——网关重放/重试导致的重复消息直接丢弃，
        # 避免重复 ACK + 重复回复。无 msg_id 时退化为不拦截（保守）。
        if msg_id and self._is_duplicate_msg(msg_id):
            logger.info(
                "wechat_bot.msg_duplicate_dropped from_user={} msg_id={}",
                from_user_id[:16], msg_id[:12],
            )
            return

        # 缓存上下文（send_message 回复时使用）——per-user 隔离（W4 修复）
        if from_user_id:
            self._last_from_user_id = from_user_id
            if context_token:
                self._ctx_by_user[from_user_id] = context_token
        elif context_token and self._last_from_user_id:
            # 消息未带 from_user_id 时兜底：沿用最近用户（仅更新 token）
            self._ctx_by_user[self._last_from_user_id] = context_token

        # W3：per-user 串行锁——同一用户的消息串行处理，不同用户并发。
        # 防止同用户连发消息时并发调用 AgentCore（会话/记忆上下文竞争、回复乱序）。
        if from_user_id:
            async with self._user_lock(from_user_id):
                await self._process_message_locked(msg, from_user_id)
        else:
            await self._process_message_locked(msg, from_user_id)

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        """获取（并按需创建）指定用户的串行锁，附带 TTL 清理。"""
        now = time.time()
        # 定期清理过期锁，避免长期运行内存线性增长
        if len(self._user_locks) > 128:
            expired = [
                k for k, ts in self._user_locks_ts.items() if now - ts > self._USER_LOCK_TTL
            ]
            for k in expired:
                self._user_locks.pop(k, None)
                self._user_locks_ts.pop(k, None)
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        self._user_locks_ts[user_id] = now
        return lock

    async def _process_message_locked(self, msg: dict, from_user_id: str) -> None:
        """持有 per-user 锁时处理消息（原 _process_message 主体）。"""
        context_token = msg.get("context_token", "") or ""

        item_list = msg.get("item_list", []) or []
        if not item_list:
            logger.info("wechat_bot.empty_item_list from_user={}", from_user_id[:16])
            return

        for item in item_list:
            if not isinstance(item, dict):
                logger.warning("wechat_bot.item_not_dict item={}", str(item)[:120])
                continue
            item_type = item.get("type", 0)
            if item_type == 1:
                # 文本消息：提取 text_item.text
                text_item = item.get("text_item", {}) or {}
                if not isinstance(text_item, dict):
                    logger.warning(
                        "wechat_bot.text_item_not_dict from_user={}",
                        from_user_id[:16],
                    )
                    continue
                text = (text_item.get("text") or "").strip()
                if not text:
                    logger.debug(
                        "wechat_bot.empty_text_content from_user={}",
                        from_user_id[:16],
                    )
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
            logger.info(
                "wechat_bot.sending_ack to_user={} ack_text={}",
                from_user_id[:16], ack_text[:40],
            )
            ack_sent = await self.send_message(
                ack_text,
                to_user_id=from_user_id,
                context_token=context_token,
            )
            logger.info(
                "wechat_bot.ack_sent to_user={} sent={}",
                from_user_id[:16], ack_sent,
            )
        except Exception as e:
            logger.warning("wechat_bot.ack_send_failed error={}", str(e)[:200])

        logger.info(
            "wechat_bot.calling_core_process user_id={} text_len={}",
            user_id, len(text),
        )
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
        if not reply:
            logger.info("wechat_bot.reply_empty user_id={}", user_id)
            return
        sticker_path = getattr(result, "sticker_path", None) or ""
        logger.info(
            "wechat_bot.reply_ready user_id={} reply_len={} has_sticker={} sticker_path={}",
            user_id, len(reply), bool(sticker_path), sticker_path,
        )
        # 先发文本回复（保证用户一定能看到回复）
        logger.info(
            "wechat_bot.sending_text_reply user_id={} reply_len={}",
            user_id, len(reply),
        )
        text_sent = await self.send_message(
            reply,
            to_user_id=from_user_id,
            context_token=context_token,
        )
        logger.info(
            "wechat_bot.text_reply_sent user_id={} sent={}",
            user_id, text_sent,
        )
        # 再发表情包（纯图，独立消息）：失败不回退文本，避免重复发送
        if sticker_path and Path(sticker_path).exists():
            logger.info(
                "wechat_bot.sticker_send_try user_id={} sticker_path={}",
                user_id, sticker_path,
            )
            sticker_sent = await self.send_media_message(
                "",
                sticker_path,
                to_user_id=from_user_id,
                context_token=context_token,
            )
            if sticker_sent:
                logger.info(
                    "wechat_bot.sticker_sent user_id={} reply_len={}",
                    user_id, len(reply),
                )
            else:
                logger.warning(
                    "wechat_bot.sticker_send_failed user_id={}",
                    user_id,
                )
        else:
            logger.info(
                "wechat_bot.no_sticker_use_text user_id={} has_sticker={}",
                user_id, bool(sticker_path),
            )

    def _on_msg_task_done(self, task: asyncio.Task) -> None:
        """消息处理任务完成回调：从集合移除并记录未捕获异常。

        若任务被 GC 回收，异常会静默丢失；这里显式取出异常并记录。
        """
        self._msg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "wechat_bot.msg_task_failed error={}",
                str(exc)[:200],
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
        # 目前仅支持文本消息，拒绝不支持的 msg_type
        if msg_type != "text":
            logger.warning("wechat_bot.send_unsupported_msg_type type={}", msg_type)
            return False

        if self._ilink_client is None:
            logger.warning("wechat_bot.send_no_client content={}", content[:80])
            return False

        target_user = to_user_id or self._last_from_user_id
        target_token = context_token or self._ctx_by_user.get(target_user, "")
        if not target_user:
            logger.warning("wechat_bot.send_no_user_id content={}", content[:80])
            return False

        _t0 = time.time()
        logger.debug(
            "wechat_bot.send_start to={} content_len={} token_len={} msg_type={}",
            target_user[:16], len(content), len(target_token), msg_type,
        )
        try:
            result = await self._ilink_client.send_message(
                target_user, target_token, content
            )
            ret = result.get("ret", 0)
            _elapsed_ms = int((time.time() - _t0) * 1000)
            logger.info(
                "wechat_bot.send_message_result to={} ret={} content_len={} elapsed_ms={}",
                target_user[:16], ret, len(content), _elapsed_ms,
            )
            return ret == 0
        except SessionExpiredError:
            # W5：不立即清除凭证——可能是服务端瞬时状态，poll 循环会指数退避重试，
            # 连续失败后才判定真正过期并清凭证。这里仅标记未连接，等待 poll 判定。
            logger.warning(
                "wechat_bot.send_session_expired elapsed_ms={} "
                "pending_poll_recovery",
                int((time.time() - _t0) * 1000),
            )
            self._connected = False
            return False
        except ILinkRetError as e:
            _elapsed_ms = int((time.time() - _t0) * 1000)
            # ret=-2: context_token 过期/无效。AgentCore 处理最长 120s，
            # 期间用户若发了新消息，per-user 缓存的 token 已更新为最新，
            # 用它重试一次可能恢复投递（覆盖"处理慢导致 token 过期"场景）。
            cached_token = self._ctx_by_user.get(target_user, "")
            if (
                e.ret == -2
                and cached_token
                and cached_token != target_token
            ):
                logger.info(
                    "wechat_bot.send_retry_with_cached_token user={} "
                    "stale_token_len={} cached_token_len={} elapsed_ms={}",
                    target_user[:16], len(target_token), len(cached_token),
                    _elapsed_ms,
                )
                _rt0 = time.time()
                try:
                    result = await self._ilink_client.send_message(
                        target_user, cached_token, content
                    )
                    ret = result.get("ret", 0)
                    _retry_ms = int((time.time() - _rt0) * 1000)
                    if ret == 0:
                        logger.info(
                            "wechat_bot.send_retry_ok user={} retry_elapsed_ms={}",
                            target_user[:16], _retry_ms,
                        )
                    else:
                        logger.warning(
                            "wechat_bot.send_retry_failed_ret_nonzero user={} ret={} retry_elapsed_ms={}",
                            target_user[:16], ret, _retry_ms,
                        )
                    return ret == 0
                except Exception as e2:
                    logger.error(
                        "wechat_bot.send_retry_failed user={} error={} retry_elapsed_ms={}",
                        target_user[:16], str(e2)[:200],
                        int((time.time() - _rt0) * 1000),
                    )
                    return False
            logger.error(
                "wechat_bot.send_error ret={} error={} elapsed_ms={}",
                e.ret, str(e)[:200], _elapsed_ms,
            )
            return False
        except Exception as e:
            logger.error(
                "wechat_bot.send_error error={} elapsed_ms={}",
                str(e)[:200],
                int((time.time() - _t0) * 1000),
            )
            return False

    async def send_media_message(
        self,
        content: str,
        image_path: str,
        to_user_id: str = "",
        context_token: str = "",
    ) -> bool:
        """发送文字+图片合并消息（表情包）。

        Args:
            content: 文本内容（可为空串，仅发图）
            image_path: 本地图片路径
            to_user_id: 目标用户（为空用缓存）
            context_token: 会话 token（为空用缓存）

        Returns:
            是否发送成功
        """
        if self._ilink_client is None:
            logger.warning("wechat_bot.send_media_no_client content={}", content[:40])
            return False
        target_user = to_user_id or self._last_from_user_id
        target_token = context_token or self._ctx_by_user.get(target_user, "")
        if not target_user:
            logger.warning("wechat_bot.send_media_no_user_id")
            return False
        try:
            result = await self._ilink_client.send_media_message(
                target_user, target_token, content, image_path
            )
            ret = result.get("ret", 0)
            logger.info(
                "wechat_bot.send_media_result to={} ret={} image_path={} content_len={}",
                target_user[:16], ret, image_path, len(content),
            )
            return ret == 0
        except SessionExpiredError:
            logger.warning("wechat_bot.send_media_session_expired")
            self._expired = True
            self._connected = False
            self._clear_credentials()
            return False
        except Exception as e:
            logger.error(
                "wechat_bot.send_media_error error={} type={} image_path={}",
                str(e)[:200], type(e).__name__, image_path,
            )
            return False

    async def send_sticker(self, sticker_path: str) -> bool:
        """发送微信表情包（仅图片，无文字）。

        Args:
            sticker_path: 表情包文件路径

        Returns:
            是否发送成功
        """
        return await self.send_media_message("", sticker_path)

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
    # 凭证持久化（委托给模块级函数，供路由层直接使用）
    # ------------------------------------------------------------------

    def _save_credentials(
        self,
        bot_token: str,
        ilink_bot_id: str,
        ilink_user_id: str,
        baseurl: str,
    ) -> None:
        """保存凭证到 ~/.ai-agent/wechat_credentials.json（委托给模块级函数）"""
        save_credentials(bot_token, ilink_bot_id, ilink_user_id, baseurl)

    def _load_credentials(self) -> Optional[dict]:
        """从 ~/.ai-agent/wechat_credentials.json 加载凭证（委托给模块级函数）"""
        return load_credentials()

    def _clear_credentials(self) -> None:
        """删除凭证文件（委托给模块级函数）"""
        clear_credentials()

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
