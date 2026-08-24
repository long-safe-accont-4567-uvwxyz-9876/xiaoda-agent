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
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from config_constants import env_flag

try:
    from utils.atomic_write import _restrict_file_permissions_windows, atomic_write
except Exception:  # pragma: no cover
    atomic_write = None  # type: ignore[assignment]
    def _restrict_file_permissions_windows(path):  # type: ignore[no-redef]
        return

from channel_adapter_base import (
    ChannelAdapterBase,
    CoreProcessRequest,
    TTLCache,
    clear_json_credentials,
    load_json_credentials,
    save_json_credentials,
)
from ilink_client import ILinkClient, ILinkRetError, SessionExpiredError

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
    薄包装：实际落盘逻辑复用 channel_adapter_base.save_json_credentials
    （原子写入 + 0600 权限 + 陈旧游标清理），行为与原实现逐字节等价。

    Args:
        bot_token: iLink Bearer token
        ilink_bot_id: 登录后的 bot ID
        ilink_user_id: 登录后的 user ID
        baseurl: 服务端下发的活跃 base URL
    """
    data = {
        "bot_token": bot_token,
        "ilink_bot_id": ilink_bot_id,
        "ilink_user_id": ilink_user_id,
        "baseurl": baseurl,
    }
    # 写入新凭证意味着新会话开始，清除陈旧游标，避免服务端按旧游标
    # 重放上一会话的历史积压消息（串话/重复回复根因之一）。
    save_json_credentials(
        CREDENTIALS_PATH,
        data,
        cursor_path=CREDENTIALS_PATH.with_name("wechat_cursor.json"),
        event="wechat_bot",
    )


def load_credentials() -> Optional[dict]:
    """从 ~/.ai-agent/wechat_credentials.json 加载凭证

    模块级函数，供路由层直接调用，无需创建 WeChatBotAdapter 实例。
    薄包装：实际读取逻辑复用 channel_adapter_base.load_json_credentials
    （损坏文件容错），行为与原实现逐字节等价。

    Returns:
        凭证字典（含 bot_token/ilink_bot_id/ilink_user_id/baseurl），
        文件不存在或无效时返回 None
    """
    return load_json_credentials(CREDENTIALS_PATH, required_key="bot_token", event="wechat_bot")


def clear_credentials() -> None:
    """删除凭证文件

    模块级函数，供路由层直接调用，无需创建 WeChatBotAdapter 实例。
    薄包装：实际删除逻辑复用 channel_adapter_base.clear_json_credentials，
    行为与原实现逐字节等价。
    """
    clear_json_credentials(CREDENTIALS_PATH, event="wechat_bot")

# iLink 默认服务端地址（登录后可能被 baseurl 覆盖）
ILINK_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

# 当前活跃的 bot 实例（对齐 qq_bot_adapter 的 _ACTIVE_BOT 模式，
# 供同进程内主动消息入口使用）
_ACTIVE_BOT: "WeChatBotAdapter | None" = None


def _get_ctx_cache(bot: "WeChatBotAdapter") -> TTLCache:
    values = bot._ctx_by_user
    stamps = getattr(bot, "_ctx_by_user_ts", None)
    if stamps is None:
        now = time.time()
        stamps = {key: now for key in values}
        bot._ctx_by_user_ts = stamps
    ttl = getattr(bot, "_CTX_TTL", 3600)
    max_size = getattr(bot, "_CTX_MAX", 256)
    cache = getattr(bot, "_ctx_cache", None)
    if cache is None or cache.values is not values or cache.stamps is not stamps:
        cache = TTLCache(values, stamps, ttl=ttl, max_size=max_size)
        bot._ctx_cache = cache
    return cache


def get_active_bot() -> "WeChatBotAdapter | None":
    """返回当前活跃的微信 adapter 实例；web 层经此读取，勿直读模块私有状态。"""
    return _ACTIVE_BOT


async def send_proactive_message(text: str,
                                 sticker_path: str | Path | None = None) -> bool:
    """向最近微信私聊用户主动发一条消息（供 web/greeting_scheduler 等调用）。

    可选携带表情包：与主对话一致——先发正文，再单发一张纯图表情包
    （iLink 协议不支持图文合并；表情发送失败不回退文本、不中断投递）。

    微信 iLink 协议要求 context_token 才能路由消息，该 token 只能在
    bot 轮询收到用户消息时缓存。因此主动发送依赖活跃 bot 实例
    （_ACTIVE_BOT 已按 per-user 缓存最近 _last_from_user_id 及其 _ctx_by_user token）。

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
    target_token = _get_ctx_cache(bot).get(bot._last_from_user_id, "")
    if not target_token:
        raise RuntimeError(
            "还没有可用的微信用户上下文（等用户先在微信上发一条消息，Bot 收到后才能主动回复）"
        )
    ok = await bot.send_message(text)
    if not ok:
        raise RuntimeError("微信消息发送失败（ret != 0）")
    _sticker = Path(sticker_path) if sticker_path else None
    if _sticker is not None and _sticker.exists():
        sticker_ok = await bot.send_media_message(
            "",
            str(_sticker),
            to_user_id=bot._last_from_user_id,
            context_token=target_token,
        )
        if sticker_ok:
            logger.info("wechat_bot.proactive_sticker_sent to={} sticker={}",
                        bot._last_from_user_id[:16], _sticker.name)
        else:
            logger.warning("wechat_bot.proactive_sticker_failed to={} sticker={}",
                           bot._last_from_user_id[:16], _sticker.name)
    logger.info("wechat_bot.proactive_sent to={} text={}", bot._last_from_user_id[:16], text[:40])
    return True

# 串行化 start() 中活跃实例的 check→stop→assign 过渡，
# 避免并发 start() 交错产生多个 poller 或覆盖未停止的旧实例。
#
# M3：不能在 import 期创建 asyncio.Lock()——它会绑定到 import 时的（或无）
# 事件循环，在测试/重启等跨 loop 场景复用会抛 "bound to a different event
# loop"。改为 per-loop 惰性创建：每个运行中的事件循环各持一把锁，
# 用 threading.Lock 保护映射的读写。
# Minor#3（R3）：用 WeakKeyDictionary 存放 loop→lock，事件循环被回收时
# 条目自动消失，避免多 loop 环境下字典无界增长（内存泄漏）。
_START_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)
_START_LOCKS_GUARD = threading.Lock()


@dataclass
class WxProcessRequest(CoreProcessRequest):
    """微信管道请求：附加回复所需的 context_token（iLink 协议路由必需）。"""

    context_token: str = ""


def _get_start_lock() -> "asyncio.Lock":
    """返回绑定当前运行事件循环的 start 锁（首次使用时惰性创建）。"""
    loop = asyncio.get_running_loop()
    with _START_LOCKS_GUARD:
        lock = _START_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _START_LOCKS[loop] = lock
        return lock


class WeChatBotAdapter(ChannelAdapterBase):
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

        # T1（cursor 先提交后处理）：
        # - _batch_gathers：当前批次 gather 句柄，poll 被取消时一并取消；
        # - _task_msg_ids：消息任务 → msg_id 映射，供 stop() 死信标记与
        #   异常终态死信标记使用（重放去重的唯一依据）。
        self._batch_gathers: set[asyncio.Future] = set()
        self._task_msg_ids: dict[asyncio.Task, str] = {}

        # 长轮询游标（首次为空字符串，后续传入上次返回的 get_updates_buf）
        # 持久化到凭证同目录：进程重启后恢复游标，避免服务端重放历史消息。
        self._cursor: str = self._load_cursor()

        # 最近一条消息的上下文（send_message 回复时使用）
        # W1 修复：改为 per-user 映射，避免多用户并发覆盖导致串话/发错人。
        # m2：附带时间戳与上限，超时/超量时清理，避免长期运行无界增长（镜像 _user_locks）。
        self._ctx_by_user: dict[str, str] = {}
        self._ctx_by_user_ts: dict[str, float] = {}
        self._CTX_TTL = 3600  # 上下文缓存 1 小时
        self._CTX_MAX = 256   # 硬上限，超出时按时间戳淘汰最旧项
        self._ctx_cache = TTLCache(
            self._ctx_by_user,
            self._ctx_by_user_ts,
            ttl=self._CTX_TTL,
            max_size=self._CTX_MAX,
        )
        self._last_from_user_id: str = ""

        # 消息去重缓存：msg_id → 时间戳，保留最近 1 小时（见 ChannelAdapterBase）。
        # 仅做 msg_id 级去重（同一 msg_id 的精确重复才拦截），不做内容级去重。
        self._init_dedup_state()

        # W3：per-user 串行锁——同一用户消息串行处理，不同用户并发（对齐 qq_bot_adapter）。
        # 防止同用户连发消息时并发调用 AgentCore 导致会话上下文竞争、回复乱序。
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._user_locks_ts: dict[str, float] = {}
        self._USER_LOCK_TTL = 3600  # 锁缓存 1 小时，超时清理避免长期运行内存增长
        self._user_locks_cache = TTLCache(
            self._user_locks,
            self._user_locks_ts,
            ttl=self._USER_LOCK_TTL,
        )

        # W5：会话过期（ret=-14）自动恢复——服务端瞬时状态时指数退避重试，
        # 连续多次失败才判定 token 真正失效。避免一次抖动就清凭证强制人工重扫码。
        self._expire_retries = 0
        self._MAX_EXPIRE_RETRIES = 3

        # A2：per-user session_id 内存缓存（对齐 qq_bot_adapter._c2c_session_cache 语义）：
        # from_user_id → session_id，TTL 1 小时 + FIFO 上限，避免每条消息都查 DB。
        self._user_session_cache: dict[str, str] = {}
        self._user_session_cache_ts: dict[str, float] = {}
        self._USER_SESSION_CACHE_TTL = 3600  # 缓存有效期 1 小时
        self._USER_SESSION_CACHE_MAX_SIZE = 1000

        # A2：per-user 最近一次 status_callback 状态（微信无事件总线用户通道，
        # 仅记录不发送）：from_user_id → 状态文本，TTL 清理防无界增长。
        self._last_status_by_user: dict[str, str] = {}
        self._last_status_by_user_ts: dict[str, float] = {}
        self._USER_STATUS_TTL = 3600
        self._last_status_cache = TTLCache(
            self._last_status_by_user,
            self._last_status_by_user_ts,
            ttl=self._USER_STATUS_TTL,
        )

        logger.info(
            "wechat_bot.init user={}",
            user_openid[:8] if user_openid else "unknown",
        )

    # -- 连接状态公开口径（ChannelAdapterBase 契约；外部禁止直读私有字段） --

    @property
    def is_connected(self) -> bool:
        return bool(self._connected)

    @property
    def is_session_expired(self) -> bool:
        return bool(self._expired)

    @property
    def has_init_failed(self) -> bool:
        return bool(self._init_failed)

    @property
    def is_polling(self) -> bool:
        t = self._poll_task
        return t is not None and not t.done()

    # ------------------------------------------------------------------
    # 消息去重与游标持久化
    # ------------------------------------------------------------------

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
        # R3-Major#1：写入前校验 token 归属——若凭证文件已被重新扫码更新为
        # 新 token（旧 poller 正在退出/即将过期），不得把旧会话游标回写，
        # 否则新会话会带上旧游标导致服务端重放历史消息（重复回复）。
        if not self._client_owns_credentials():
            logger.info(
                "wechat_bot.cursor_save_skipped_token_changed cursor_len={}",
                len(self._cursor),
            )
            return
        try:
            path = self._cursor_path()
            content = json.dumps({"cursor": self._cursor}, ensure_ascii=False)
            if atomic_write is not None:
                # Minor#4（R3）：游标含会话状态信息，权限对齐凭证文件（0600），
                # 避免 umask 默认权限（如 0644）导致同机其他用户可读。
                atomic_write(path, content, mode=0o600, encoding="utf-8")
            else:
                # fallback: 固定 tmp 方式（atomic_write 不可用时）
                tmp = path.with_suffix(".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.chmod(tmp, 0o600)  # Unix: 限制为仅用户可读写
                _restrict_file_permissions_windows(tmp)  # Windows: 用 ACL 补偿
                tmp.replace(path)
        except Exception as e:
            logger.warning("wechat_bot.cursor_save_failed error={}", str(e)[:120])

    def _client_owns_credentials(self) -> bool:
        """校验凭证文件中的 bot_token 与当前 client 的 token 是否一致。

        R3-Major#1：重新扫码会写入新 token；旧 poller 判断"会话过期"时若直接
        删凭证 / 回写游标，会破坏新凭证（删掉刚写入的 T2）或写回旧游标。
        仅当凭证文件 token 与当前 client token 一致（确实是本实例的会话）
        才允许删除凭证 / 持久化游标。
        """
        client = self._ilink_client
        if client is None:
            return False
        client_token = getattr(client, "_bot_token", "") or ""
        if not client_token:
            return False
        try:
            creds = load_credentials()
        except Exception as e:
            logger.warning("wechat_bot.creds_ownership_check_failed error={}", str(e)[:120])
            return False
        if creds is None:
            return False
        return creds.get("bot_token", "") == client_token

    def _remember_ctx(self, user_id: str, token: str) -> None:
        """记录 per-user 上下文 token（m2：带 TTL + 上限清理，避免无界增长）。

        清理策略统一委托 :meth:`TTLCache.prune_pairs`（原手搓「先剔过期 → 写入 →
        逐个淘汰最旧」三段式的等价收敛：写入后一次性执行过期剔除 + 最旧淘汰，
        终态一致），消除与 QQ 会话缓存清理逻辑的漂移温床。
        """
        if not user_id or not token:
            return
        cache = _get_ctx_cache(self)
        cache.set(user_id, token)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> "dict[str, Any]":
        """启动微信 Bot 适配器，返回结构化 readiness 判定（T4）。

        - 检测凭证文件 ~/.ai-agent/wechat_credentials.json
        - 有凭证：用凭证初始化 ILinkClient，直接启动消息轮询
        - 无凭证：等待 WebUI 触发扫码流程（不自动启动轮询）
        - 设置 _ACTIVE_BOT = self（对齐 qq_bot_adapter 的模式）

        Returns:
            readiness dict：{"ok": bool, "connected": bool, "polling": bool,
            "error": str}。ok=True 表示适配器已连接且轮询任务在跑；调用方
            （/wechat/start 路由、server 自动恢复）必须仅在 ok=True 时把本
            实例挂载为活跃 bot——旧实现吞掉初始化错误并回滚 disconnected 却
            正常返回，导致路由层无条件挂载"僵尸实例"。

        兼容性：内部状态口径（_connected/_running/is_polling 等）不变；
        返回值由 None 变为 dict——旧调用方忽略返回值时行为不受影响。
        """

        def _readiness(ok: bool, error: str = "") -> dict[str, Any]:
            return {
                "ok": bool(ok and self._connected and self.is_polling),
                "connected": bool(self._connected),
                "polling": bool(self.is_polling),
                "error": str(error or ""),
            }

        global _ACTIVE_BOT
        # 串行化活跃实例的 check→stop→assign 过渡：并发 start() 交错会产生
        # 多个 poll_task 或覆盖未停止的旧实例（"消息重复 N 次"根因之一）。
        # W2 修复：_running/_closed 状态赋值必须在锁内完成——
        # 否则并发 start 时旧实例被 stop 后，锁外代码会把 _closed 覆盖回 False，
        # 继续拉起来第二个 poller（同凭证同游标 → 同条消息双份 ACK + 双份回复）。
        async with _get_start_lock():
            # 幂等：同一实例已在运行则直接返回，避免重复建 poller / 覆盖 client
            if _ACTIVE_BOT is self and self._running and not self._closed:
                logger.info("wechat_bot.start_already_running")
                return _readiness(True)
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

        # 确保 AgentCore 已初始化（未提供时尝试自建）
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
                # m1：成功初始化后复位失败标志，避免残留的旧故障状态误报。
                self._init_failed = False
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
            return _readiness(False, "adapter stopped concurrently during start")

        # m9：成功启动到此处即认为一次干净的生命周期开始，复位过期重试计数，
        # 避免上次会话遗留的退避计数让新会话过早判定"真过期"。
        self._expire_retries = 0

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
                return _readiness(True)
            except Exception as e:
                logger.error(
                    "wechat_bot.ilink_init_failed error={}",
                    str(e)[:200],
                )
                self._connected = False
                # R3-Major#2：ILinkClient 初始化失败同样视为未就绪——
                # 回滚 _ACTIVE_BOT/_running，避免僵尸实例（auto-start not-ready
                # 分支只检查 app.state.wechat_bot，僵尸会让 /wechat/stop 失效）。
                if _ACTIVE_BOT is self:
                    _ACTIVE_BOT = None
                self._running = False
                self._closed = True
                # T4：不再吞错回滚后正常返回——如实上报失败详情
                return _readiness(False, f"ILinkClient init failed: {e}")
        else:
            # 无凭证：等待 WebUI 触发扫码流程（不自动启动轮询）
            # R3-Major#2：回滚 start() 早期无条件设置的 _ACTIVE_BOT/_running，
            # 避免留下"僵尸实例"——_ACTIVE_BOT 指向未运行实例会让 /wechat/test
            # 短路读状态、/wechat/stop 无法停止真正运行的 poller（auto-start
            # not-ready 时 poller 可能稍后恢复）。
            logger.info("wechat_bot.no_credentials waiting_for_scan")
            if _ACTIVE_BOT is self:
                _ACTIVE_BOT = None
            self._running = False
            self._closed = True
            return _readiness(False, "no credentials (scan QR code to log in)")

    def _start_polling(self) -> None:
        """启动消息轮询任务（幂等：已运行时不重复创建）。"""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_messages())
        # Minor#5（R3）：挂 done callback——poll 循环若以未捕获异常退出
        # （如事件循环关闭时 create_task 抛 RuntimeError），取回异常并复位
        # 连接状态，避免 /wechat/status 误报 connected=True 且无自动恢复。
        self._poll_task.add_done_callback(self._on_poll_task_done)

    def _on_poll_task_done(self, task: "asyncio.Task") -> None:
        """poll task 结束回调：取回异常、收敛状态，避免静默停摆。"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return  # 正常取消（stop() 主动 cancel），无需处理
        except Exception:
            exc = None
        if exc is not None:
            logger.error(
                "wechat_bot.poll_task_crashed error={} type={}",
                str(exc)[:200], type(exc).__name__,
            )
            self._connected = False

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

        # T1：批次收尾 gather 若仍在等待（poll 已死但批次未终结），一并取消，
        # 避免 stop 后仍有悬空 await 挂着"未提交游标"的批次。
        for gather in list(self._batch_gathers):
            gather.cancel()
        self._batch_gathers.clear()

        # 取消未完成的消息处理任务（best-effort，避免断开后仍跑 AgentCore 至 120s）。
        # T1 契约：取消前先按 msg_id 记死信——这些消息的游标尚未推进，重启后服务端
        # 会按旧游标重放本批；死信让重放被去重拦截，保证"不丢消息也不重复回复"。
        if self._msg_tasks:
            for task in list(self._msg_tasks):
                if not task.done():
                    task.cancel()
                    self._mark_task_msg_dead(task)
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
                    # M4：真正过期后收敛到干净终态，使 /wechat/status 与重启逻辑
                    # 口径一致（不再残留 _running=True / 悬空 client/task 引用）。
                    await self._converge_terminal_expired()
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

            # T1：先取新游标但**不提交**——等批次内全部消息终结后再推进。
            next_cursor = result.get("cursor", "") or ""

            # m3：不在轮询批次层面绑定 context_token——一个批次可能包含多个用户的
            # 消息，批次级 token 绑到 _last_from_user_id 会 shadow per-user 的正确绑定
            # （串话根因）。上下文 token 一律由 _process_message 按各自 from_user_id 绑定。

            # 分发消息（用 asyncio.create_task 避免阻塞轮询）
            msgs = result.get("msgs", []) or []
            if msgs:
                logger.info(
                    "wechat_bot.poll_received poll_count={} elapsed_ms={} msg_count={} cursor_len={}",
                    _poll_count, _elapsed_ms, len(msgs), len(next_cursor),
                )
                batch_tasks = []
                for msg in msgs:
                    task = asyncio.create_task(self._process_message(msg))
                    self._msg_tasks.add(task)
                    msg_id = str(msg.get("msg_id", "") or "") if isinstance(msg, dict) else ""
                    if msg_id:
                        self._task_msg_ids[task] = msg_id
                    task.add_done_callback(self._on_msg_task_done)
                    batch_tasks.append(task)
                # T1（cursor 先提交后处理缺陷）：批次内全部消息终结（成功/异常/死信）
                # 之后才推进游标。旧实现在创建任务前就持久化 cursor，崩溃/停机会把
                # "已提交未处理"的整批消息永久丢失；新实现崩溃重启后服务端按旧游标
                # 重放本批，stop() 路径取消的任务已记死信（去重拦截重复回复）。
                gather = asyncio.gather(*batch_tasks, return_exceptions=True)
                self._batch_gathers.add(gather)
                try:
                    outcomes = await asyncio.shield(gather)
                except asyncio.CancelledError:
                    # poll 被取消（stop/过期收敛）：取消批次收尾，游标不推进，
                    # 未完成消息的死信标记由 stop() 统一处理。
                    gather.cancel()
                    raise
                finally:
                    self._batch_gathers.discard(gather)
                # 单条异常视为该条终结：记死信（重放不重复处理），不卡整批游标。
                for task, outcome in zip(batch_tasks, outcomes or []):
                    if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                        logger.error(
                            "wechat_bot.msg_task_failed error={}",
                            str(outcome)[:200],
                        )
                        self._mark_msg_dead_by_task(task)

            # 推进游标（get_updates_buf）：为空时保留原游标，避免重放历史消息。
            # W1：持久化游标，进程重启后恢复，避免服务端按空游标重放历史消息。
            # T1：仅当本批次无消息、或批次内全部消息已终结时才会走到这里。
            if next_cursor:
                self._cursor = next_cursor
                self._save_cursor()

            if not msgs:
                # 无消息时短暂 sleep，避免紧密循环
                await asyncio.sleep(0.5)

        logger.info(
            "wechat_bot.poll_stopped expired={} running={} total_polls={}",
            self._expired,
            self._running,
            _poll_count,
        )

    async def _converge_terminal_expired(self) -> None:
        """会话确认过期后收敛到干净终态（M4）。

        poll 循环内调用：不 cancel/await 自身 poll_task（会自等死锁），
        仅置生命周期标志、关闭并释放 client、释放 poll_task/_ACTIVE_BOT 引用，
        使 /wechat/status 与 /wechat/start 重启逻辑对同一实例口径一致。
        """
        global _ACTIVE_BOT
        self._running = False
        self._connected = False
        self._expired = True
        self._closed = True
        # 关闭并释放 ILinkClient（best-effort）
        if self._ilink_client is not None:
            try:
                await self._ilink_client.close()
            except Exception as e:
                logger.warning(
                    "wechat_bot.terminal_close_error error={}",
                    str(e)[:200],
                )
            self._ilink_client = None
        # 释放 poll_task 引用（当前正是该 task 在执行，勿 cancel/await 自身）
        self._poll_task = None
        if _ACTIVE_BOT is self:
            _ACTIVE_BOT = None
        logger.info("wechat_bot.terminal_state_converged expired=True")

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
        # m2：经 _remember_ctx 写入，带 TTL + 上限清理避免无界增长。
        if from_user_id:
            self._last_from_user_id = from_user_id
            self._remember_ctx(from_user_id, context_token)
        elif context_token and self._last_from_user_id:
            # 消息未带 from_user_id 时兜底：沿用最近用户（仅更新 token）
            self._remember_ctx(self._last_from_user_id, context_token)

        # W3：per-user 串行锁——同一用户的消息串行处理，不同用户并发。
        # 防止同用户连发消息时并发调用 AgentCore（会话/记忆上下文竞争、回复乱序）。
        if from_user_id:
            async with self._user_lock(from_user_id):
                await self._process_message_locked(msg, from_user_id)
        else:
            await self._process_message_locked(msg, from_user_id)

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        """获取（并按需创建）指定用户的串行锁，附带 TTL 清理。

        清理、查找和刷新严格按 ``prune → lookup/create → timestamp refresh``
        执行，保证目标用户的过期锁即使是缓存唯一条目也不会被复用。
        """
        cache = getattr(self, "_user_locks_cache", None)
        if cache is None or cache.values is not self._user_locks:
            cache = TTLCache(
                self._user_locks,
                self._user_locks_ts,
                ttl=self._USER_LOCK_TTL,
            )
            self._user_locks_cache = cache
        cache.prune()
        lock = cache.get(user_id, prune=False)
        if lock is None:
            lock = asyncio.Lock()
        cache.set(user_id, lock, prune=False)
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

    # ------------------------------------------------------------------
    # A2：per-user session 管理（实现下沉 ChannelAdapterBase，QQ/微信共用）
    # ------------------------------------------------------------------

    def _prune_user_session_cache(self) -> None:
        """清理用户 session 缓存中的过期与超限条目。"""
        self._session_cache_prune(
            self._user_session_cache, self._user_session_cache_ts,
            ttl=self._USER_SESSION_CACHE_TTL, max_size=self._USER_SESSION_CACHE_MAX_SIZE)

    def _set_user_session_cache(self, user_id: str, sid: str) -> None:
        """统一缓存写入 + 立即执行 size cap。"""
        self._session_cache_set(
            self._user_session_cache, self._user_session_cache_ts,
            user_id, sid,
            ttl=self._USER_SESSION_CACHE_TTL, max_size=self._USER_SESSION_CACHE_MAX_SIZE)

    async def _get_or_create_user_session(self, from_user_id: str) -> str:
        """获取或创建用户会话 session_id（统一实现在 ChannelAdapterBase）。

        - 内存缓存优先（TTL 1 小时 + FIFO 上限），避免每条消息都查 DB
        - 检测到 wechat_tmp_ 兜底 ID 时视为缓存失效，跳过缓存继续查 DB
          （对齐 QQ P1-7：临时 ID 不存在于 sessions 表，继续缓存会导致上下文永久丢失）
        - DB 异常/超时兜底 wechat_tmp_{from_user_id[:16]}，总 deadline 20s
        """
        return await self._get_or_create_session_cached(
            from_user_id,
            core=self._core,
            cache=self._user_session_cache,
            cache_ts=self._user_session_cache_ts,
            ttl=self._USER_SESSION_CACHE_TTL,
            max_size=self._USER_SESSION_CACHE_MAX_SIZE,
            tmp_prefix="wechat_tmp_",
            log_prefix="wechat_bot",
            event_stem="user_session",
        )

    # ------------------------------------------------------------------
    # A2：status_callback 最近状态记录（微信无事件总线用户通道，仅记录不发送）
    # ------------------------------------------------------------------

    def _prune_last_status_cache(self) -> None:
        """清理过期状态条目（TTL 1 小时），避免长期运行内存线性增长。"""
        self._get_last_status_cache().prune()

    def _get_last_status_cache(self) -> TTLCache:
        cache = getattr(self, "_last_status_cache", None)
        if cache is None or cache.values is not self._last_status_by_user:
            cache = TTLCache(
                self._last_status_by_user,
                self._last_status_by_user_ts,
                ttl=self._USER_STATUS_TTL,
            )
            self._last_status_cache = cache
        return cache

    def _remember_last_status(self, user_id: str, status: str) -> None:
        """记录用户最近一次中间状态并清理过期条目。"""
        self._get_last_status_cache().set(user_id, status)

    # ------------------------------------------------------------------
    # B2：ChannelAdapterBase._process_with_core 骨架的微信侧钩子
    # （ACK/session/status_callback/兜底文案与 QQ 语义不同，全部经钩子消化）
    # ------------------------------------------------------------------

    #: 微信原实现对 process 阶段捕获所有异常（宽于 QQ 的四类窄集）
    CORE_ERROR_TYPES: tuple[type[BaseException], ...] = (Exception,)

    def _get_core(self) -> Any:
        return self._core

    async def _send_ack(self, req: WxProcessRequest) -> None:
        """处理前立即发送"收到啦，正在想"（对齐 QQ 行为）；失败容忍继续处理。"""
        try:
            from emotion.emoji_config import get_ack_message
            ack_text = get_ack_message("xiaoda")
            logger.info(
                "wechat_bot.sending_ack to_user={} ack_text={}",
                req.user_openid[:16], ack_text[:40],
            )
            ack_sent = await self.send_message(
                ack_text,
                to_user_id=req.user_openid,
                context_token=req.context_token,
            )
            logger.info(
                "wechat_bot.ack_sent to_user={} sent={}",
                req.user_openid[:16], ack_sent,
            )
        except Exception as e:
            logger.warning("wechat_bot.ack_send_failed error={}", str(e)[:200])

    async def _resolve_session(self, req: WxProcessRequest) -> str | None:
        """先取/建 per-user session（内存缓存优先，DB 异常兜底临时 ID）。"""
        session_id = await self._get_or_create_user_session(req.user_openid)
        logger.debug(
            "wechat_bot.session_ready user={} session_id={}",
            req.user_openid[:16], session_id[:32],
        )
        return session_id

    def _make_status_callback(self, req: WxProcessRequest) -> Any:
        """微信无事件总线用户通道：仅记 DEBUG 日志 + 维护 per-user 最近状态。"""
        from_user_id = req.user_openid

        async def status_notify(msg: str) -> None:
            logger.debug(
                "wechat_bot.status_notify user={} status={}",
                from_user_id[:16], str(msg)[:200],
            )
            self._remember_last_status(from_user_id, str(msg))

        return status_notify

    async def _on_core_timeout(self, req: WxProcessRequest) -> None:
        logger.warning("wechat_bot.process_timeout user_id={}", req.user_id)
        await self.send_message(
            "处理超时，请稍后再试",
            to_user_id=req.user_openid,
            context_token=req.context_token,
        )

    async def _on_core_error(self, req: WxProcessRequest, exc: BaseException) -> None:
        logger.error(
            "wechat_bot.process_error user_id={} error={}",
            req.user_id,
            str(exc)[:200],
            exc_info=True,
        )
        await self.send_message(
            "出了点小问题，等会儿再聊好不好？",
            to_user_id=req.user_openid,
            context_token=req.context_token,
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

        # ── /whoami 自助查询（对齐 QQ 通道）──────────────────────
        # 微信 C2C 没有"拉群者"这种可信绑定信号，主人身份须显式绑定。
        # 用户发 /whoami 即可拿到自己的 from_user_id，再填进 .env 的
        # MASTER_WECHAT_OPENID 完成绑定（security 层会将其并入 owner_ids）。
        if text.strip() == "/whoami":
            reply = (
                f"你的微信用户 ID（from_user_id）是：\n{from_user_id}\n\n"
                f"在 .env 的「MASTER_WECHAT_OPENID」填入此值即可绑定主人身份"
                f"（多个用逗号分隔）。绑定后重启服务生效。"
            )
            try:
                await self.send_message(reply, to_user_id=from_user_id,
                                        context_token=context_token)
            except Exception as e:
                logger.warning("wechat_bot.whoami_send_failed error={}", str(e)[:200])
            return

        user_id = f"wechat_{from_user_id}" if from_user_id else "wechat_unknown"
        logger.info("wechat_bot.text_msg user_id={} text={}", user_id, text[:80])

        logger.info(
            "wechat_bot.calling_core_process user_id={} text_len={}",
            user_id, len(text),
        )
        # B2：ACK → session → status_callback → wait_for(process,120) → 超时/异常兜底
        # 统一走 ChannelAdapterBase._process_with_core 骨架（原逐段复制的管道沉淀层）。
        req = WxProcessRequest(
            text=text,
            user_id=user_id,
            source="wechat_c2c",
            user_openid=from_user_id,
            context_token=context_token,
        )
        result = await self._process_with_core(req)
        if result is None:
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
        # A2：分段流式发送（对齐 QQ C2C 流式）——~300 字符/片、最多 4 片，
        # 超出尾部合并后按字节重切；单片时走原有单条 send_message 路径（行为不变）。
        segments = self._split_text_for_streaming(reply, 300)
        segments = self._cap_stream_segments(
            segments, False,
            "wechat_bot.stream_capped_resplit", "wechat_bot.stream_capped")
        if len(segments) <= 1:
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
        else:
            logger.info(
                "wechat_bot.stream_start user_id={} reply_len={} segments={}",
                user_id, len(reply), len(segments),
            )

            async def _wx_send(content: str) -> bool:
                return await self.send_message(
                    content,
                    to_user_id=from_user_id,
                    context_token=context_token,
                )

            async def _recover(index: int, failure: Any) -> None:
                remaining = "".join(segments[index:])
                pieces = self._split_text_by_bytes(remaining, 7800)
                for piece in pieces:
                    try:
                        ok = await _wx_send(piece)
                    except Exception as exc:
                        logger.warning(
                            "wechat_bot.stream_recovery_exception size={} error={}",
                            len(piece),
                            str(exc)[:200],
                        )
                        break
                    if ok is False:
                        logger.warning(
                            "wechat_bot.stream_recovery_failed size={}",
                            len(piece),
                        )
                        break

            # B2：共享层只负责段间节奏与逐片发送；微信恢复含当前片，
            # 按 7800 UTF-8 字节重切，任一恢复片失败即停止。
            await self._send_segments_paced(
                segments,
                _wx_send,
                on_failure=_recover,
                log_prefix="wechat_bot",
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
        # T1：仅成功终结的任务清理 msg_id 映射；取消/异常终态的映射保留，
        # 由 stop() 死信标记与批次收尾（异常死信）负责消费。
        if not task.cancelled():
            exc = task.exception()
            if exc is None:
                self._task_msg_ids.pop(task, None)
            elif exc is not None:
                logger.error(
                    "wechat_bot.msg_task_failed error={}",
                    str(exc)[:200],
                )
        if task.cancelled():
            return

    def _mark_msg_dead_by_task(self, task: asyncio.Task) -> None:
        """按任务取回 msg_id 并记入去重表（死信），幂等：无映射时静默跳过。"""
        msg_id = self._task_msg_ids.get(task, "")
        if msg_id:
            # 记死信：该消息未完成处理即被取消/失败，重放时按去重拦截
            self._processed_msg_ids.setdefault(msg_id, time.time())
            self._task_msg_ids.pop(task, None)

    def _mark_task_msg_dead(self, task: asyncio.Task) -> None:
        """:meth:`_mark_msg_dead_by_task` 的 stop() 别名（同步、不取回任务结果）。"""
        self._mark_msg_dead_by_task(task)

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
            context_token: 会话上下文 token（为空时按 target_user 从 _ctx_by_user 取缓存）

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
        target_token = context_token or _get_ctx_cache(self).get(target_user, "")
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
            cached_token = _get_ctx_cache(self).get(target_user, "")
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
        target_token = context_token or _get_ctx_cache(self).get(target_user, "")
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
            # W5：与 send_message 对齐——不立即清除凭证，也不置 _expired。
            # 可能是服务端瞬时状态，poll 循环会指数退避重试，连续失败后才判定
            # 真正过期并清凭证。这里仅标记未连接，等待 poll 判定恢复。
            logger.warning(
                "wechat_bot.send_media_session_expired pending_poll_recovery"
            )
            self._connected = False
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
        """删除凭证文件（委托给模块级函数）。

        R3-Major#1：先校验 token 归属——仅当凭证文件 token 与当前 client
        一致（本实例的会话真正过期）才删除；若凭证已被重新扫码更新为新 token，
        旧 poller 不得删除（否则新登录态被误删，用户被迫重新扫码）。
        """
        if not self._client_owns_credentials():
            logger.info("wechat_bot.clear_credentials_skipped_token_changed")
            return
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
    enabled = env_flag("WECHAT_ILINK_ENABLED", False)
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
