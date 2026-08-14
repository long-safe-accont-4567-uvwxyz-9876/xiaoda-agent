from typing import Any
import os
import sys
import asyncio
import base64
import contextvars
import sqlite3
import subprocess
import threading
import time
import random
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv
# P0 修复（Windows 安装包 QQ 离线 bug 根因）：
# load_dotenv() 无参数时只读取 CWD/.env，Windows 安装包从 C:\Program Files\ 启动时
# CWD 不是用户目录，而 config.py 已把 .env 放到 ~/.ai-agent/.env（frozen 模式），
# 导致 APP_ID/APP_SECRET 永远为空 → run_qq_bot 早期返回 disabled_no_appid → QQ 离线。
# 修复：显式使用 config.ENV_PATH，与 config.py 保持一致。
try:
    from config import ENV_PATH as _ENV_PATH
    load_dotenv(_ENV_PATH, override=True)
except ImportError:
    # config 模块不可用时兜底（如独立运行模式），退化为无参数 load_dotenv
    load_dotenv()


from utils.common import safe_int as _safe_int, safe_float as _safe_float
from utils.llm_cleanup import strip_qq_face_tags

# 安全加固：不再全局 monkey patch ssl.create_default_context
# botpy 内部已使用 SSLContext() 处理 WebSocket SSL，无需全局禁用证书验证

from utils.logging_config import setup_logging
setup_logging()

from loguru import logger

import botpy
from botpy.gateway import BotWebSocket
from botpy.message import C2CMessage, GroupMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_core import AgentCore, ProcessResult
from agent_core.user_qq import QQUser
from core.event_bus import event_bus
from config import AGENT_CONFIG, get_agent_display_name
from security.human_approval import (
    IMApprovalChannel, ApprovalRequest, ApprovalStatus,
    RiskLevel, HIGH_RISK_OPERATIONS,
)
from emotion.nudge_engine import NudgeEngine
from emotion.emoji_config import get_ack_message
from utils.text_utils import encode_image_to_base64

_original_is_system_event = BotWebSocket._is_system_event

async def _patched_is_system_event(self, message_event: Any, ws: Any) -> Any:
    event_op = message_event.get("op")
    if event_op == BotWebSocket.WS_HEARTBEAT_ACK:
        self._last_heartbeat_ack = asyncio.get_running_loop().time()
    return await _original_is_system_event(self, message_event, ws)

BotWebSocket._is_system_event = _patched_is_system_event

_original_send_heart = BotWebSocket._send_heart

async def _patched_send_heart(self, interval: Any) -> None:
    _log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    _log.info("[botpy] 心跳维持启动（带超时检测）...")
    self._last_heartbeat_ack = asyncio.get_running_loop().time()
    missed_acks = 0
    while True:
        if self._conn is None:
            _log.debug("[botpy] 连接已关闭!")
            return
        if self._conn.closed:
            _log.debug("[botpy] ws连接已关闭, 心跳检测停止")
            return

        # 先发送心跳（捕获连接关闭异常，避免心跳任务失败导致 QQ Bot 断连）
        payload = {
            "op": self.WS_HEARTBEAT,
            "d": self._session["last_seq"],
        }
        try:
            await self.send_msg(__import__("json").dumps(payload))
        except Exception as e:
            # WebSocket 已关闭或网络异常，心跳任务退出（QQ Bot SDK 会自动重连）
            _log.warning(f"[botpy] 心跳发送失败，连接可能已关闭: {e}")
            return
        await asyncio.sleep(interval)

        # 再检查 ACK 是否超时
        now = asyncio.get_running_loop().time()
        if now - self._last_heartbeat_ack > interval * 4:
            missed_acks += 1
            _log.warning(f"[botpy] 心跳ACK超时 ({missed_acks}次), 上次ACK: {int(now - self._last_heartbeat_ack)}秒前")
            if missed_acks >= 3:
                _log.warning("[botpy] 心跳ACK连续超时，强制断开重连!")
                await self._conn.close()
                return
        else:
            missed_acks = 0

BotWebSocket._send_heart = _patched_send_heart

# Patch: on_closed 时处理 session 失效码（4009 等），强制 IDENTIFY 重连
# 根因：botpy _INVALID_RECONNECT_CODE=[9001,9005] 不含 4009(Session timed out)，
#   导致 4009 后 session_id 未清空 → 重连用 ws_resume → RESUME 已超时 session 无效
#   → QQ 网关接受连接但不推送消息 → bot 在线却收不到任何用户消息（严重阻塞）。
#   实测：07-28 16:20 起 每30分钟 4009+ws_resume，从未 ws_identify，消息零接收。
# 修复：4009/4007 时清空 session_id，重连走 ws_identify 重建 session（QQ 官方要求）。
# CodeRabbit 修复：4008 是限频（rate limited），session 仍有效，应保留 session_id
#   走 ws_resume（RESUME）。原实现把 4008 纳入 _SESSION_INVALID_CODES 清空 session_id
#   → 重连走 ws_identify → 丢失未 ACK 的消息（RESUME 本可恢复）。
_original_on_closed = BotWebSocket.on_closed

async def _patched_on_closed(self, close_status_code: Any, close_msg: Any) -> Any:
    _SESSION_INVALID_CODES = {4007, 4009}  # session 失效，必须重新 IDENTIFY
    _botpy_log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    if close_status_code in _SESSION_INVALID_CODES:
        _botpy_log.warning(
            f"[botpy] session失效(code={close_status_code})，清空session强制IDENTIFY重连")
        self._session["session_id"] = ""
        self._session["last_seq"] = 0
    elif close_status_code == 4008:
        # 4008 限频：session 仍有效，保留 session_id 走 RESUME（不丢未 ACK 消息）
        # botpy 自带 session_interval backoff，不强行 sleep 避免与重连机制冲突
        _botpy_log.warning(
            f"[botpy] 限频(code=4008)，保留session走RESUME，等待botpy backoff重连")
    await _original_on_closed(self, close_status_code, close_msg)

BotWebSocket.on_closed = _patched_on_closed

from botpy.client import Client as _BotpyClient

_original_pool_init = _BotpyClient._pool_init


async def _patched_pool_init(self, token: Any, session_interval: Any) -> Any:
    _botpy_log = __import__("botpy.logging", fromlist=["get_logger"]).get_logger()
    for i in range(self._ws_ap["shards"]):
        session = {
            "session_id": "",
            "last_seq": 0,
            "intent": self.intents,
            "token": token,
            "url": self._ws_ap["url"],
            "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
        }
        self._connection.add(session)

    loop = self._connection.loop

    def _loop_exception_handler(_loop: Any, context: Any) -> None:
        _loop.default_exception_handler(context)
        exception = context.get("exception")
        if isinstance(exception, ZeroDivisionError):
            _loop.stop()

    loop.set_exception_handler(_loop_exception_handler)

    recon_attempts = 0
    max_recon_delay = 60

    while not self._closed:
        _botpy_log.debug("[botpy] 会话循环检查...")
        try:
            # multi_run 是 async def，返回的协程对象恒为 truthy——
            # 原 `if coroutine: ... else: 重新登录` 的 else 分支永不执行（死代码）。
            # Q2 修复：删除死分支，直接 await；连接中断后由 bot_connect 内部
            # ws 重连机制（RESUME/IDENTIFY）接管，外层仅维持循环与异常退避。
            coroutine = self._connection.multi_run(session_interval)
            if self.ret_coro:
                return coroutine
            await coroutine
            recon_attempts = 0
            if not self._closed:
                await asyncio.sleep(0.1)
        except (TimeoutError, OSError, RuntimeError, ConnectionError) as e:
            recon_attempts += 1
            delay = min(5 * (2 ** min(recon_attempts - 1, 4)), max_recon_delay)
            _botpy_log.error(f"[botpy] 会话异常: {e}, {delay}秒后重试 (第{recon_attempts}次)")
            await asyncio.sleep(delay)
            try:
                await self._bot_login(token)
                for i in range(self._ws_ap["shards"]):
                    session = {
                        "session_id": "",
                        "last_seq": 0,
                        "intent": self.intents,
                        "token": token,
                        "url": self._ws_ap["url"],
                        "shards": {"shard_id": i, "shard_count": self._ws_ap["shards"]},
                    }
                    self._connection.add(session)
            except (OSError, RuntimeError, ConnectionError) as login_err:
                _botpy_log.error(f"[botpy] 异常后重新登录失败: {login_err}")
    return None


_BotpyClient._pool_init = _patched_pool_init

APP_ID = os.getenv("QQBOT_APP_ID", "")
APP_SECRET = os.getenv("QQBOT_APP_SECRET", "")

_qq_cfg = AGENT_CONFIG.get("qq_bot", {})
MAX_REPLY_LEN = _qq_cfg.get("max_reply_length", 8000)
QQ_C2C_MAX_SEGMENTS = 4
QQ_GROUP_MAX_SEGMENTS = 4
QQ_GROUP_MEDIA_BUDGET = 3

# HITL: Agent 输出中嵌入的高危操作标记，QQ 适配器拦截后触发两段式确认
_HIGH_RISK_OP_MARKER = "__HIGH_RISK_OP__:"
_HIGH_RISK_OP_RE = re.compile(
    r"__HIGH_RISK_OP__:\s*(\w+)(?:\s+(.*))?\s*$", re.MULTILINE)

_msg_seq_counter = int(time.time())
_msg_seq_lock = threading.Lock()
_env_write_lock = threading.Lock()
# QQ API 要求 msg_seq 为 int32 范围（0~2147483647）。实测毫秒时间戳（13 位，
# ~1.7e12）超范围被拒（40011000「请求数据异常」），导致所有回复发送失败。
# 改用秒级时间戳（10 位，2038-01-19 前安全），仍保证：1) 单调递增；
# 2) 时钟回拨/进程休眠后计数器落后时不产生回退，避免服务端拒绝。


def _next_msg_seq() -> int:
    global _msg_seq_counter
    with _msg_seq_lock:
        now_s = int(time.time())
        _msg_seq_counter = max(_msg_seq_counter + 1, now_s)
        return _msg_seq_counter


def _save_master_openid(openid: str) -> None:
    """将 openid 追加到 MASTER_QQ_OPENID（逗号分隔），并更新运行时环境变量。"""
    with _env_write_lock:
        existing = os.getenv("MASTER_QQ_OPENID", "").strip()
        ids = [x.strip() for x in existing.split(",") if x.strip()]
        if openid in ids:
            return
        ids.append(openid)
        value = ",".join(ids)

        from pathlib import Path
        # frozen 模式下 .env 在用户目录 ~/.ai-agent/.env
        try:
            from config import ENV_PATH
            env_path = Path(ENV_PATH)
        except ImportError:
            env_path = Path(__file__).parent / ".env"
        if not env_path.exists():
            env_path.write_text(f"MASTER_QQ_OPENID={value}\n", encoding="utf-8-sig")
        else:
            lines = env_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
            found = False
            for i, line in enumerate(lines):
                if line.strip().startswith("MASTER_QQ_OPENID="):
                    lines[i] = f"MASTER_QQ_OPENID={value}\n"
                    found = True
                    break
            if not found:
                lines.append(f"\nMASTER_QQ_OPENID={value}\n")
            env_path.write_text("".join(lines), encoding="utf-8-sig")
        os.environ["MASTER_QQ_OPENID"] = value
        logger.info("qq_bot.master_openid_saved", openid=openid, total=len(ids))


def _parse_master_ids() -> list[str]:
    """解析 MASTER_QQ_OPENID 环境变量为去空白的 openid 列表（逗号分隔）。"""
    raw = os.getenv("MASTER_QQ_OPENID", "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


def _build_user_input(content: str, attachment_info: str) -> str:
    """拼接文本与附件描述为用户输入。"""
    return f"{content} {attachment_info}".strip() if content else attachment_info


# 当前活跃的 bot 实例（同进程内 GreetingScheduler 等主动消息入口使用）
_ACTIVE_BOT: "AIQQBot | None" = None


async def send_proactive_message(text: str, openid: str = "") -> bool:
    """向最近私聊用户（或指定 openid）主动发一条 QQ 消息。

    供 web/greeting_scheduler 等同进程模块调用；QQ client 未连接时返回 False。
    """
    bot = _ACTIVE_BOT
    if bot is None or bot.is_closed():
        raise RuntimeError("QQ client 未连接")
    target = openid or bot._last_c2c_openid
    if not target:
        raise RuntimeError("没有可用的 QQ 用户 openid（等用户先发一条私聊，或设置 NUDGE_USER_OPENID）")
    await bot.api.post_c2c_message(
        openid=target, content=text, msg_type=0, msg_seq=_next_msg_seq())
    logger.info("qq_bot.proactive_sent openid={} text={}", target[:8], text[:40])
    return True


async def run_qq_bot(agent: "AgentCore", *, sandbox: bool = False) -> None:
    """在现有事件循环中运行 QQ client（与 WebUI 同进程模式）。

    内部带指数退避重连；任务被取消时干净退出。
    """
    # P0 修复：实时从 env 读取 APP_ID/APP_SECRET，而非依赖模块级变量。
    # 根因：模块级 APP_ID 在 import 时一次性读取，若 load_dotenv 未读到 .env（如
    # Windows 安装包 CWD 不对），APP_ID 永远为空。即使后续 restart_qq_bot_task
    # 更新了模块级变量，首次启动仍会失败。实时读取 env 确保始终拿到最新值。
    _app_id = os.getenv("QQBOT_APP_ID", "").strip() or APP_ID
    _app_secret = os.getenv("QQBOT_APP_SECRET", "").strip() or APP_SECRET
    if not _app_id or _app_id == "your_app_id_here":
        logger.warning("qq_bot.disabled_no_appid")
        return
    intents = botpy.Intents(public_messages=True)
    delay = 5
    while True:
        client = AIQQBot(intents=intents, is_sandbox=sandbox, timeout=30, agent=agent)
        try:
            # 每次重连都用最新的 env 值（防止凭证更新后仍用旧值）
            _app_id = os.getenv("QQBOT_APP_ID", "").strip() or APP_ID
            _app_secret = os.getenv("QQBOT_APP_SECRET", "").strip() or APP_SECRET
            await client.start(appid=_app_id, secret=_app_secret)
            logger.warning("qq_bot.exited_reconnecting")
            delay = 5
        except asyncio.CancelledError:
            try:
                await client.close()
            except (OSError, RuntimeError) as e:
                logger.warning(f"qq_bot.close_on_cancel_failed: {e}")
            raise
        except Exception as e:
            logger.error("qq_bot.crashed_retrying error={} delay={}", str(e)[:200], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


class AIQQBot(botpy.Client):
    """QQ 机器人适配器，处理消息接收、去重与 AgentCore 调用。"""
    def __init__(self, *args: Any, agent: "AgentCore | None" = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 支持注入共享的 AgentCore（与 WebUI 同进程同实例），未注入则自建（独立运行模式）
        self.agent = agent or AgentCore()
        self._agent_shared = agent is not None
        self.nudge_engine = None
        # 消息去重缓存：msg_id → 时间戳，保留最近 1 小时
        self._processed_msg_ids: dict[str, float] = {}
        self._MSG_ID_TTL = 3600  # 1 小时
        # 注：用户明确要求"我发送的内容不需要去重"——
        # 不做内容级去重（即使网关重连重投递导致重复回复，也不拦截用户手动重发）。
        # 仅保留 msg_id 级去重（同一 msg_id 的精确重复才拦截）。
        self._agent_initialized = agent is not None and getattr(agent, "_initialized", False)
        # 最近一个私聊用户 openid，主动消息（问候同步）发给该用户
        self._last_c2c_openid: str = os.getenv("NUDGE_USER_OPENID", "")
        # C2C session_id 内存缓存：user_openid → session_id
        # 根因：单连接 SQLite + WAL 模式下，并发写操作会阻塞读，
        #       导致 get_active_session 超时 5 秒触发 c2c_session_timeout。
        # 修复：首次成功后缓存 session_id，避免每条消息都查 DB；session 失效时降级到查 DB。
        # 加固: TTL 过期清理 + FIFO 上限避免长期运行内存无限增长；process 异常时主动失效。
        self._c2c_session_cache: dict[str, str] = {}
        # per-user asyncio.Lock：同一用户消息串行，不同用户并发（避免堵塞）
        self._c2c_locks: dict[str, asyncio.Lock] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._c2c_session_cache_ttl = 3600  # 缓存有效期 1 小时
        self._c2c_session_cache_ts: dict[str, float] = {}
        # P1-1: 缓存上限，超过时按 FIFO 淘汰最旧条目（防多用户长期运行内存泄漏）
        self._C2C_SESSION_CACHE_MAX_SIZE = 1000
        # HITL: 高危操作两段式确认（默认开启，QQ_HITL_ENABLED=false 关闭）
        self.hitl_enabled = os.getenv("QQ_HITL_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        self.im_approval = IMApprovalChannel(
            send_callback=self._send_approval_message,
            timeout=_safe_float(os.getenv("QQ_HITL_TIMEOUT", "60"), 60),
        )
        self._approval_message_ctx = self._new_approval_context()
        global _ACTIVE_BOT
        _ACTIVE_BOT = self

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        now = time.time()
        # 清理过期项
        expired = [k for k, ts in self._processed_msg_ids.items() if now - ts > self._MSG_ID_TTL]
        for k in expired:
            del self._processed_msg_ids[k]
        # 检查重复
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids[msg_id] = now
        return False

    @staticmethod
    def _new_approval_context() -> contextvars.ContextVar[Any]:
        return contextvars.ContextVar("qq_approval_message", default=None)

    @staticmethod
    def _cleanup_message_lock(locks: dict[str, asyncio.Lock], key: str) -> None:
        lock = locks.get(key)
        if lock is None:
            return
        # 仅当锁未被持有且无等待者时才清理（R3 观察项）：
        # 若仅检查 locked()，锁刚释放但仍有 task 在 acquire 队列等待时
        # pop 掉旧锁，后续新消息会创建新锁，与排队者并行处理同一用户
        # 消息 → per-user 串行锁失效。
        waiters = getattr(lock, "_waiters", None)
        if not lock.locked() and not waiters:
            locks.pop(key, None)

    def _prune_c2c_session_cache(self) -> None:
        """P1-1: 清理 C2C session 缓存中的过期与超限条目。

        1. 删除超过 TTL 的过期条目（避免永久驻留）
        2. 超过 MAX_SIZE 时按 FIFO（最早 ts）淘汰最旧条目（防多用户长期运行内存泄漏）
        """
        now = time.time()
        # 1. 清理过期条目
        expired = [
            k for k, ts in self._c2c_session_cache_ts.items()
            if now - ts > self._c2c_session_cache_ttl
        ]
        for k in expired:
            self._c2c_session_cache.pop(k, None)
            self._c2c_session_cache_ts.pop(k, None)
        # 2. FIFO 淘汰超限条目
        overflow = len(self._c2c_session_cache) - self._C2C_SESSION_CACHE_MAX_SIZE
        if overflow > 0:
            # 按 ts 升序排序，删除最早的 overflow 个
            sorted_keys = sorted(self._c2c_session_cache_ts.items(), key=lambda kv: kv[1])
            for k, _ in sorted_keys[:overflow]:
                self._c2c_session_cache.pop(k, None)
                self._c2c_session_cache_ts.pop(k, None)

    def _invalidate_c2c_session(self, user_openid: str) -> None:
        """P1-2: 主动失效指定用户的 session_id 缓存。

        场景: agent.process 抛错（session 失效、被删除等）时调用，
        保证下次消息重新查 DB 获取最新 session_id。
        """
        self._c2c_session_cache.pop(user_openid, None)
        self._c2c_session_cache_ts.pop(user_openid, None)

    def _set_c2c_session_cache(self, user_openid: str, sid: str) -> None:
        """CodeRabbit F8: 统一缓存写入 + 立即执行 size cap。

        替代分散的 `cache[k]=v; ts[k]=time.time()` 模式，确保写入后
        立即淘汰 overflow，不依赖下次 _get_or_create 的 pre-lookup prune。
        """
        self._c2c_session_cache[user_openid] = sid
        self._c2c_session_cache_ts[user_openid] = time.time()
        # 写入后立即 cap，保证不变量 len(cache) <= MAX_SIZE 始终成立
        self._prune_c2c_session_cache()

    @staticmethod
    def _get_config_service() -> Any:
        try:
            from web.config_service import get_config_service
            return get_config_service()
        except (ImportError, AttributeError):
            logger.debug("qq_bot_adapter.config_service_not_found", exc_info=True)
            return None

    async def on_ready(self) -> None:
        # R3 观察项：实时读取 env 而非模块级 APP_ID——模块级变量在 import 时
        # 一次性读取，.env 后更新时日志会显示过时/错误的 app_id。
        _live_app_id = os.getenv("QQBOT_APP_ID", "").strip() or APP_ID
        logger.info("qq_bot.connected", app_id=_live_app_id)

        try:
            if not self._agent_initialized:
                await self.agent.init()
                self._agent_initialized = True
                logger.info("qq_bot.agent_initialized")
            else:
                logger.info("qq_bot.reconnected_agent_reused")
        except Exception as e:
            logger.error("qq_bot.agent_init_failed", error=str(e)[:300], exc_info=True)
            # 不重抛，避免 botpy 将此视为 on_ready 异常而断开 WebSocket

        nudge_enabled = os.getenv("NUDGE_ENABLED", "false").lower() == "true"
        if nudge_enabled and self.nudge_engine is None:
            user_openid = os.getenv("NUDGE_USER_OPENID", "")
            if user_openid:
                try:
                    self.nudge_engine = NudgeEngine(
                        db=self.agent.db,
                        analytics=self.agent.db.analytics,
                        router=self.agent.router,
                        api=self.api,
                        user_openid=user_openid,
                        greeting_threshold=_safe_int(os.getenv("NUDGE_GREETING_THRESHOLD", "3600"), 3600),
                        dnd_start=_safe_int(os.getenv("NUDGE_DND_START", "23"), 23),
                        dnd_end=_safe_int(os.getenv("NUDGE_DND_END", "8"), 8),
                        portrait_manager=self.agent.portrait_manager,
                        config_service=self._get_config_service(),
                        core=self.agent,
                    )
                    await self.nudge_engine.start()
                    # 恢复 nudge 功能节点的后端/本地模型选择（on_ready 晚于 lifespan 的
                    # restore，若 bot 未就绪 restore 会跳过，这里在就绪后补一次恢复）
                    try:
                        from web.local_deploy_nodes import get_backend, get_local_model
                        _cfg = self._get_config_service()
                        if _cfg is not None:
                            _nudge_backend = get_backend(_cfg, "nudge")
                            _nudge_model = get_local_model(_cfg, "nudge") or None
                            self.nudge_engine.set_backend(_nudge_backend, _nudge_model)
                    except Exception as _e:
                        logger.debug("nudge.backend_restore_failed error={}", _e)
                except (ImportError, AttributeError, OSError, RuntimeError) as e:
                    logger.warning("nudge.init_failed", error=str(e))

        if self.nudge_engine:
            self.nudge_engine.poke()

    async def on_error(self, event_name: Any, *args: Any, **kwargs: Any) -> None:
        """botpy 事件处理出错时的回调。

        botpy _run_event 出错时调用 self.on_error(event_name, *args, **kwargs)，
        旧签名 (self, error) 只接受 1 个参数导致 TypeError，异常被吞掉
        （"Task exception was never retrieved"），消息处理静默失败。
        """
        import traceback
        tb = traceback.format_exc()
        logger.error(
            "qq_bot.event_error",
            event=str(event_name)[:100],
            error=str(args[0])[:200] if args else "",
            traceback=tb[:500] if tb and tb != "NoneType: None\n" else "",
        )

    async def on_close(self, close_status_code: Any, close_msg: Any) -> None:
        logger.warning("qq_bot.ws_closed", code=close_status_code, msg=str(close_msg)[:100])
        # 注意：不在 on_close 中调用 agent.shutdown()
        # 因为 on_close 在临时断开时也会触发，而外层重连循环会复用同一实例
        # shutdown 会释放数据库等资源，导致重连后 Agent 不可用
        # shutdown 应在程序真正退出时调用

    async def _send_approval_message(self, text: str) -> None:
        """通过当前消息上下文发送审批确认请求消息（供 IMApprovalChannel 回调）。"""
        msg = self._approval_message_ctx.get()
        if msg is None:
            logger.warning("qq_bot.approval_no_message_context text=%s", text[:80])
            return
        try:
            await msg.reply(content=text, msg_seq=_next_msg_seq())
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.warning("qq_bot.approval_send_failed error=%s", str(e)[:200])

    async def _check_high_risk_approval(self, result: ProcessResult, message: Any,
                                          user_id: str, is_owner: bool) -> ProcessResult:
        """检查 Agent 输出是否包含高危操作标记，若是则触发两段式确认。

        - 检测 `__HIGH_RISK_OP__: <operation> <args>` 标记
        - 调用 IMApprovalChannel.request_approval 等待用户确认
        - 确认通过：去除标记后继续发送回复
        - 取消/超时：替换回复为"已取消"
        """
        if not self.hitl_enabled:
            return result
        reply = result.reply or ""
        if _HIGH_RISK_OP_MARKER not in reply:
            return result
        match = _HIGH_RISK_OP_RE.search(reply)
        if not match:
            return result
        operation = match.group(1)
        args_str = (match.group(2) or "").strip()
        risk_level = HIGH_RISK_OPERATIONS.get(operation, RiskLevel.HIGH)
        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            user_id=user_id,
            operation=operation,
            args={"raw": args_str},
            risk_level=risk_level,
            reason=f"High-risk operation: {operation}",
        )
        token = self._approval_message_ctx.set(message)
        try:
            status = await self.im_approval.request_approval(req, is_owner=is_owner)
        finally:
            self._approval_message_ctx.reset(token)
        if status in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED):
            # 确认通过：去除标记后继续发送
            result.reply = _HIGH_RISK_OP_RE.sub("", reply).strip() or "✅ 高危操作已确认"
        else:
            # 取消或超时
            result.reply = "⚠️ 高危操作已取消"
        return result

    async def _process_message_attachments(self, message: Any) -> tuple[list[dict], str]:
        """处理消息中的附件，返回图片数据和附件描述文本。

        遍历消息附件，接收文件、编码图片为 base64，生成附件描述文本。
        C2C 消息和群消息的附件处理逻辑完全一致，提取此方法消除重复。

        Args:
            message: QQ Bot 消息对象（C2CMessage 或 GroupMessage）

        Returns:
            tuple[list[dict], str]: (image_data, attachment_info)
                image_data: 图片的 mimeType+base64 列表，供视觉识别使用
                attachment_info: 附件描述文本，拼接到用户输入中
        """
        image_data = []
        attachment_info = ""
        if hasattr(message, 'attachments') and message.attachments:
            parts = []
            for att in message.attachments:
                ct = getattr(att, 'content_type', '') or ''
                fn = getattr(att, 'filename', '') or ''
                result = await self.agent.receive_file(att)
                if result["status"] == "ok":
                    if result.get("text_preview"):
                        parts.append(f"[文件: {fn}]\n内容预览:\n{result['text_preview'][:500]}")
                    else:
                        if ct.startswith("image/"):
                            save_path = result.get('save_path', '')
                            parts.append(f"[图片: {fn}，已保存到 {save_path}]")
                            try:
                                mime, img_b64 = encode_image_to_base64(save_path)
                                image_data.append({"mimeType": mime, "data": img_b64})
                            except (FileNotFoundError, ValueError):
                                pass
                            except (OSError, ValueError, RuntimeError) as e:
                                logger.warning("qq_bot.image_encode_failed", error=str(e))
                        else:
                            parts.append(f"[文件: {fn}，已保存到 {result['save_path']}]")
                else:
                    if ct.startswith("image/"):
                        parts.append(f"[图片: {fn or 'image'}]")
                    elif ct.startswith("video/"):
                        parts.append(f"[视频: {fn or 'video'}]")
                    else:
                        parts.append(f"[附件: {fn or 'unknown'}]")
            attachment_info = " ".join(str(p) for p in parts)
        return image_data, attachment_info

    async def on_group_add_robot(self, event: Any) -> None:
        """机器人被拉入群时，自动将拉入者绑定为主人。"""
        op_openid = getattr(event, "op_member_openid", "")
        group_openid = getattr(event, "group_openid", "")
        if not op_openid:
            logger.warning("qq_bot.group_add_robot.no_openid", group=group_openid)
            return
        logger.info("qq_bot.group_add_robot", group=group_openid, op_openid=op_openid)
        _save_master_openid(op_openid)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        parsed = await self._parse_c2c_message(message)
        if parsed is None:
            return
        content, image_data, user_input, user_openid, user_id = parsed

        is_master = self._identify_c2c_master(user_openid)
        if not is_master:
            logger.info("qq_bot.non_master_message", user_id=user_id, openid=user_openid, content=user_input[:80])

        if self.nudge_engine:
            self.nudge_engine.poke()

        msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
        if msg_id and self._is_duplicate_msg(msg_id):
            return

        if await self._handle_c2c_quick_commands(content, message, user_openid, user_id):
            return

        # 并发处理消息（per-user 锁保证同一用户串行，不同用户并发）
        if user_openid not in self._c2c_locks:
            self._c2c_locks[user_openid] = asyncio.Lock()

        async def _c2c_reply_with_lock() -> None:
            try:
                async with self._c2c_locks[user_openid]:
                    session_id = await self._get_or_create_c2c_session(user_openid)
                    await self._process_c2c_reply(message, user_input, user_id, user_openid, session_id, is_master, image_data)
            finally:
                self._cleanup_message_lock(self._c2c_locks, user_openid)

        # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致回复丢失，
        # 改 _spawn（跟踪引用 + 完成回收），保证用户一定收到回复。
        from core.background_tasks import _spawn
        _spawn(_c2c_reply_with_lock())

    async def _parse_c2c_message(self, message: C2CMessage) -> tuple[str, list, str, str, str] | None:
        """解析 C2C 消息内容和发送者信息。

        返回 (content, image_data, user_input, user_openid, user_id)，
        若消息为空（无文本且无附件）返回 None。
        """
        content = (getattr(message, 'content', None) or "").strip()
        content = strip_qq_face_tags(content)  # 剥离 QQ 表情标签，防止污染 LLM 上下文被模仿
        image_data, attachment_info = await self._process_message_attachments(message)
        if not content and not attachment_info:
            return None
        user_input = f"{content} {attachment_info}".strip() if content else attachment_info

        user_openid = getattr(message.author, 'user_openid', '') if hasattr(message, 'author') else ''
        user_id = f"qq_{user_openid}" if user_openid else "qq_unknown"
        if user_openid:
            self._last_c2c_openid = user_openid
        logger.info("qq_bot.c2c_message", user_id=user_id, openid=user_openid, content=user_input[:80])
        return content, image_data, user_input, user_openid, user_id

    def _identify_c2c_master(self, user_openid: str) -> bool:
        """识别发送者是否为主人。

        安全策略：不再"首个私聊者自动绑主"。若 MASTER_QQ_OPENID 未配置，
        所有私聊用户均视为非主人（fail-closed），并通过 /whoami 引导用户
        在 Setup Wizard 中显式录入主人 openid。这避免公开部署时任意第一个
        私聊者窃取主人权限。
        """
        master_raw = os.getenv("MASTER_QQ_OPENID", "").strip()
        master_ids = [x.strip() for x in master_raw.split(",") if x.strip()]
        is_master = bool(master_ids) and user_openid in master_ids
        if not is_master and user_openid and not master_ids:
            # 仅记录一次警告，引导用户去 Setup Wizard 配置主人 openid
            if not getattr(self, "_warned_no_master", False):
                logger.warning(
                    "qq_bot.master_not_configured url=setup_wizard "
                    "hint=run /whoami to read openid, then set MASTER_QQ_OPENID"
                )
                self._warned_no_master = True
        return is_master

    async def _get_or_create_c2c_session(self, user_openid: str) -> str:
        """获取或创建会话，失败时返回空字符串。添加超时保护防止 DB 锁长期阻塞消息处理。

        优化：使用内存缓存避免每条消息都查 DB。
        根因：单连接 SQLite + WAL 模式下，并发写操作会阻塞读，
              导致 get_active_session 超时 5 秒触发 c2c_session_timeout（212 次错误）。
        修复：首次成功后缓存 session_id 1 小时，避免重复查询；
              仅在缓存失效或会话不存在时才查 DB。
        """
        # P1-1: 入口先清理过期与超限条目，避免长期运行内存泄漏
        self._prune_c2c_session_cache()
        # 1. 先查内存缓存
        cached_sid = self._c2c_session_cache.get(user_openid)
        cached_ts = self._c2c_session_cache_ts.get(user_openid, 0)
        # P1-7 修复：检测 cached_sid 以 "qq_tmp_" 开头时视为缓存失效，跳过缓存继续查 DB。
        # 根因：DB 超时/异常时返回 qq_tmp_{openid[:16]} 兜底 session_id，该 ID 不存在于
        # sessions 表，后续 background_tasks 的 UPDATE 零行生效不报错，所有消息都写到
        # 不存在的 session，上下文永久丢失。检测到 qq_tmp_ 时跳过缓存让下次能恢复真实 session。
        if (cached_sid
                and not cached_sid.startswith("qq_tmp_")
                and (time.time() - cached_ts < self._c2c_session_cache_ttl)):
            return cached_sid

        # 2. 缓存未命中，查 DB
        # 总截止时间 20s（略大于 busy_timeout 15s），所有尝试共享此额度，
        # 避免重试时重置超时导致总延迟达 40s（CodeRabbit finding）
        deadline = time.monotonic() + 20.0
        try:
            session = await asyncio.wait_for(
                self.agent.get_session(user_openid),
                timeout=max(deadline - time.monotonic(), 0.1),
            )
            if session:
                sid = session["id"]
                self._set_c2c_session_cache(user_openid, sid)
                return sid
            # 没有活跃会话，创建新会话
            sid = await asyncio.wait_for(
                self.agent.create_session(user_openid),
                timeout=max(deadline - time.monotonic(), 0.1),
            )
            self._set_c2c_session_cache(user_openid, sid)
            return sid
        except (TimeoutError, sqlite3.OperationalError) as e:
            logger.warning("qq_bot.c2c_session_db_error openid={} error={}, retrying", user_openid, str(e)[:100])
            # DB 锁/超时后用剩余时间重试一次（锁通常是短暂的）
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error("qq_bot.c2c_session_deadline_exhausted openid={}", user_openid)
                return f"qq_tmp_{user_openid[:16]}"
            try:
                session = await asyncio.wait_for(
                    self.agent.get_session(user_openid),
                    timeout=remaining,
                )
                if session:
                    sid = session["id"]
                    self._set_c2c_session_cache(user_openid, sid)
                    return sid
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error("qq_bot.c2c_session_deadline_exhausted openid={}", user_openid)
                    return f"qq_tmp_{user_openid[:16]}"
                sid = await asyncio.wait_for(
                    self.agent.create_session(user_openid),
                    timeout=remaining,
                )
                self._set_c2c_session_cache(user_openid, sid)
                return sid
            except (TimeoutError, sqlite3.OperationalError) as e2:
                logger.error("qq_bot.c2c_session_db_error_retry openid={} error={}", user_openid, str(e2)[:100])
                # 关键修复：DB 超时/锁时返回临时 session_id，保证消息不丢失
                # 后续 agent.process 仍能执行，仅持久化能力受影响
                fallback_sid = f"qq_tmp_{user_openid[:16]}"
                return fallback_sid
        except (KeyError, OSError, RuntimeError) as e:
            logger.error(f"qq_bot.c2c_session_failed: {e}")
            # 同样返回临时 session_id，避免消息丢失
            return f"qq_tmp_{user_openid[:16]}"

    async def _handle_c2c_quick_commands(self, content: str, message: C2CMessage,
                                          user_openid: str, user_id: str) -> bool:
        """处理快速指令（/whoami、HITL 审批回复）。返回 True 表示已处理，跳过正常流程。"""
        # /whoami 指令：回复发送者的 openid（用于主人在 Setup 中填写）
        if content.strip() in ("/whoami", "/whoami "):
            await message.reply(content=f"你的 OpenID 是：\n{user_openid}\n\n在 Setup 配置页面的「主人 QQ OpenID」填入此值即可绑定主人身份。", msg_seq=_next_msg_seq())
            return True
        # HITL: 若用户有待审批请求，先尝试匹配回复（"确认"/"取消"），匹配则跳过正常处理
        if self.hitl_enabled:
            approval_user = user_openid or user_id
            if await self.im_approval.handle_user_reply(approval_user, content):
                return True
        return False

    async def _process_c2c_reply(self, message: C2CMessage, user_input: str, user_id: str,
                                  user_openid: str, session_id: str, is_master: bool,
                                  image_data: list) -> None:
        """发送 ACK、调用 agent 处理消息并回复，处理超时与异常。"""
        try:
            await message.reply(content=get_ack_message('xiaoda'), msg_seq=_next_msg_seq())

            async def status_notify(msg) -> None:
                # 所有中间状态消息（工具状态、进度提示等）不发送到 QQ
                # 实际回复通过 _send_reply_with_sticker / _send_streaming_reply 发送
                return

            # 绑定 QQUser 到 EventBus
            async def _qq_reply(content: str, msg_seq: int = 0) -> None:
                response = await self.api.post_c2c_message(
                    openid=user_openid,
                    content=content,
                    msg_type=0,
                    msg_seq=msg_seq,
                )
                if response is None:
                    raise RuntimeError("C2C状态消息接口返回None")
            qq_user = QQUser(reply_fn=_qq_reply, msg_seq_fn=_next_msg_seq)
            token = event_bus.bind_user(qq_user)
            try:
                result = await asyncio.wait_for(
                    self.agent.process(user_input, user_id=user_id, source="qq_c2c",
                                      user_openid=user_openid, session_id=session_id,
                                      status_callback=status_notify,
                                      image_data=image_data if image_data else None,
                                      is_master=is_master),
                    timeout=120,  # 降低兜底超时，避免长时间堵塞用户消息队列
                )
            finally:
                event_bus.unbind_user(token)
            # HITL: 高危操作两段式确认（检测 __HIGH_RISK_OP__ 标记）
            result = await self._check_high_risk_approval(
                result, message, user_openid or user_id, is_master)
            if result.reply:
                await self._send_reply_with_sticker(message, result)
        except TimeoutError:
            logger.warning("qq_bot.c2c_timeout user=%s", user_id)
            # 记录失败状态，供下次消息恢复上下文
            if hasattr(self.agent, 'context') and self.agent.context:
                self.agent.context.record_failure("处理超时", user_input)
            try:
                await message.reply(content=f"{get_agent_display_name('xiaoda')}想得太入神了……能再说一次吗？🌱", msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as _e:
                logger.debug("qq_bot.c2c_timeout_reply_failed", error=str(_e))
        except (TimeoutError, RuntimeError, OSError, ValueError) as e:
            logger.error(f"qq_bot.c2c_error: {e}")
            # P1-2: 仅在 agent 处理失败（非 QQ 网络短暂错误）时失效 session 缓存
            # RuntimeError/OSError 可能是 QQ 网络短暂错误（ACK/回复发送失败），不应清除健康缓存
            # ValueError 通常表示数据格式问题（如 session 无效），需要重新查 DB
            if user_openid and isinstance(e, ValueError):
                self._invalidate_c2c_session(user_openid)
            try:
                await message.reply(content="嗯……出了点小问题，等会儿再聊好不好？", msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error(f"qq_bot.c2c_fallback_reply_failed: {e}")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
        if msg_id and self._is_duplicate_msg(msg_id):
            return
        _group_lock_key = getattr(message, 'group_openid', '')
        if not _group_lock_key:
            _group_lock_key = "qq_unknown"
        if _group_lock_key not in self._group_locks:
            self._group_locks[_group_lock_key] = asyncio.Lock()

        async def _group_reply_with_lock() -> None:
            # Q5 修复：user_id/user_input 定义于 try 块内，_process_message_attachments
            # 等步骤抛异常时 TimeoutError/error 分支引用它们会 NameError——
            # 在闭包入口预置默认值，保证异常分支可安全引用（对齐 c2c 的参数绑定）。
            user_id = "qq_unknown"
            user_input = ""
            try:
                async with self._group_locks[_group_lock_key]:
                    content = (getattr(message, 'content', None) or "").strip()
                    content = strip_qq_face_tags(content)  # 剥离 QQ 表情标签，防止污染 LLM 上下文被模仿

                    image_data, attachment_info = await self._process_message_attachments(message)

                    if not content and not attachment_info:
                        return

                    user_input = f"{content} {attachment_info}".strip() if content else attachment_info

                    member_openid = getattr(message.author, 'member_openid', '') if hasattr(message, 'author') else ''
                    user_id = f"qq_{member_openid}" if member_openid else "qq_unknown"
                    logger.info("qq_bot.group_message", user_id=user_id, openid=member_openid, content=user_input[:80])

                    # 主人识别：对比 member_openid 与 MASTER_QQ_OPENID（逗号分隔多值）
                    # on_group_add_robot 已自动绑定拉群者的 member_openid
                    master_raw = os.getenv("MASTER_QQ_OPENID", "").strip()
                    master_ids = [x.strip() for x in master_raw.split(",") if x.strip()]
                    is_master = bool(master_ids) and member_openid in master_ids
                    if is_master:
                        logger.info("qq_bot.master_identified", member_openid=member_openid)
                    else:
                        logger.info("qq_bot.non_master_message", user_id=user_id, openid=member_openid, content=user_input[:80])

                    if self.nudge_engine:
                        self.nudge_engine.poke()

                    # /whoami 指令：回复发送者的 openid（用于主人在 Setup 中填写）
                    if content.strip() in ("/whoami", "/whoami "):
                        await message.reply(content=f"你的 OpenID 是：\n{member_openid}\n\n在 Setup 配置页面的「主人 QQ OpenID」填入此值即可绑定主人身份。", msg_seq=_next_msg_seq())
                        return

                    # HITL: 若用户有待审批请求，先尝试匹配回复（"确认"/"取消"），匹配则跳过正常处理
                    if self.hitl_enabled:
                        approval_user = member_openid or user_id
                        if await self.im_approval.handle_user_reply(approval_user, content):
                            return

                    # 群聊被动回复 5 分钟内最多 2 次，无主动消息权限（40034105）
                    # 策略：每次都先发 ACK（1 次配额），再用单条发送回复（1 次配额）
                    # status_callback 静默（不消耗配额）
                    async def status_notify(msg: str) -> None:
                        pass

                    # 立即发送 ACK
                    try:
                        await message.reply(content=get_ack_message('xiaoda'), msg_seq=_next_msg_seq())
                    except (OSError, RuntimeError, ConnectionError) as e:
                        logger.debug("qq_bot.ack_send_failed", error=str(e))

                    # 绑定 QQUser 到 EventBus（群聊也需要子代理事件投递）
                    async def _group_reply(content: str, msg_seq: int = 0) -> None:
                        await message.reply(content=content, msg_seq=msg_seq)
                    # Q1 修复：群聊被动回复配额 5 次/5 分钟，ACK+4 片回复已占满 5 次，
                    # SUB_STARTED 通知会击穿配额触发 40034105 → 群聊禁用开始通知。
                    qq_user = QQUser(
                        reply_fn=_group_reply,
                        msg_seq_fn=_next_msg_seq,
                        notify_started=False,
                    )
                    token = event_bus.bind_user(qq_user)
                    try:
                        result = await asyncio.wait_for(
                            self.agent.process(user_input, user_id=user_id, source="qq_group",
                                              user_openid=member_openid,
                                              status_callback=status_notify,
                                              image_data=image_data if image_data else None,
                                              is_master=is_master),
                            timeout=120,  # 降低兜底超时，避免长时间堵塞用户消息队列
                        )
                    finally:
                        event_bus.unbind_user(token)
                    # HITL: 高危操作两段式确认（检测 __HIGH_RISK_OP__ 标记）
                    result = await self._check_high_risk_approval(
                        result, message, member_openid or user_id, is_master)
                    if result.reply:
                        await self._send_reply_with_sticker(message, result)
            except TimeoutError:
                logger.warning("qq_bot.group_timeout user=%s", user_id)
                if hasattr(self.agent, 'context') and self.agent.context:
                    self.agent.context.record_failure("处理超时", user_input)
                try:
                    await message.reply(content=f"{get_agent_display_name('xiaoda')}想得太入神了……能再说一次吗？🌱", msg_seq=_next_msg_seq())
                except (OSError, RuntimeError, ConnectionError) as _e:
                    logger.debug("qq_bot.group_timeout_reply_failed", error=str(_e))
            except (RuntimeError, OSError, ValueError) as e:
                logger.error(f"qq_bot.group_error: {e}", exc_info=True)
                try:
                    await message.reply(content="嗯……出了点小问题，等会儿再聊好不好？", msg_seq=_next_msg_seq())
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error(f"qq_bot.group_fallback_reply_failed: {e2}")
            finally:
                self._cleanup_message_lock(self._group_locks, _group_lock_key)


        # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致回复丢失。
        from core.background_tasks import _spawn
        _spawn(_group_reply_with_lock())
    async def _send_reply_with_media(self, message: Any, reply: str,
                                      image_path: Path | None = None,
                                      image_url: str | None = None) -> None:
        if not image_path and not image_url:
            await message.reply(content=reply, msg_seq=_next_msg_seq())
            return

        try:
            if isinstance(message, C2CMessage):
                openid = message.author.user_openid
                if image_path:
                    file_info = await self._upload_c2c_base64(openid, image_path)
                else:
                    media = await self.api.post_c2c_file(
                        openid=openid, file_type=1, url=image_url
                    )
                    file_info = getattr(media, "file_info", "")
                if not file_info:
                    raise RuntimeError("C2C媒体接口返回空file_info")
                response = await self.api.post_c2c_message(
                    openid=openid, msg_id=message.id,
                    msg_type=7, content=reply,
                    media={"file_info": file_info}, msg_seq=_next_msg_seq()
                )
                if response is None:
                    raise RuntimeError("C2C消息接口返回None")
            elif isinstance(message, GroupMessage):
                group_openid = message.group_openid
                if image_path:
                    file_info = await self._upload_group_base64(group_openid, image_path)
                else:
                    media = await self.api.post_group_file(
                        group_openid=group_openid, file_type=1, url=image_url
                    )
                    file_info = getattr(media, "file_info", "")
                if not file_info:
                    raise RuntimeError("群媒体接口返回空file_info")
                try:
                    # 被动回复（需要 msg_id）；无主动消息权限，超限直接失败
                    await self.api.post_group_message(
                        group_openid=group_openid, msg_id=message.id,
                        msg_type=7, content=reply,
                        media={"file_info": file_info}, msg_seq=_next_msg_seq()
                    )
                except (OSError, RuntimeError, ConnectionError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        # 被动回复超限，无主动消息权限，记录后跳过（不再降级为主动消息）
                        logger.warning("qq_bot.group_media_passive_limited_no_proactive",
                                       error=str(e))
                    else:
                        raise
            else:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
        except (OSError, RuntimeError, ConnectionError, ValueError) as e:
            logger.warning("qq_bot.media_send_failed", error=str(e))
            # 最终兜底：尝试纯文本回复
            try:
                await message.reply(content=reply, msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as _e:
                logger.debug("qq_bot.fallback_reply_failed", error=str(_e))

    async def _upload_c2c_base64(self, openid: str, image_path: Path, file_type: int = 1) -> str:
        return await self._upload_base64(openid, image_path, file_type, group=False)

    async def _upload_group_base64(self, group_openid: str, image_path: Path, file_type: int = 1) -> str:
        return await self._upload_base64(group_openid, image_path, file_type, group=True)

    async def _upload_base64(self, target: str, image_path: Path, file_type: int = 1,
                             *, group: bool = False) -> str:
        """上传 base64 文件到 QQ 文件接口（C2C/群聊共用，Q6 去重）。

        图片类型（file_type=1）且文件 >800KB 时自动压缩；临时文件由 finally 清理。

        Args:
            target: C2C 的 openid 或群聊的 group_openid
            image_path: 本地文件路径
            file_type: 1=图片 2=视频 3=语音
            group: True=群文件接口 /v2/groups/...，False=C2C 接口 /v2/users/...

        Returns:
            file_info 字符串

        Raises:
            RuntimeError: 3 次重试后仍失败
        """
        from botpy.http import Route

        compressed_path: Path | None = None
        try:
            def _read() -> Any:
                nonlocal compressed_path
                # 图片类型且文件过大时压缩
                path_to_upload = image_path
                if file_type == 1 and image_path.stat().st_size > 800_000:
                    compressed_path = self._compress_image(image_path)
                    path_to_upload = compressed_path
                with open(path_to_upload, "rb") as f:
                    return base64.b64encode(f.read()).decode()

            file_data = await asyncio.to_thread(_read)
            if group:
                payload = {
                    "group_openid": target,
                    "file_type": file_type,
                    "file_data": file_data,
                    "srv_send_msg": False,
                }
                route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=target)
                desc = "群文件上传"
            else:
                payload = {
                    "openid": target,
                    "file_type": file_type,
                    "file_data": file_data,
                    "srv_send_msg": False,
                }
                route = Route("POST", "/v2/users/{openid}/files", openid=target)
                desc = "C2C文件上传"
            # 重试最多3次，每次间隔递增；失败统一抛带原因的错误（原 raise last_err 依赖
            # 循环必然赋值的隐式约定，改为显式 RuntimeError 更健壮）
            last_err: BaseException | None = None
            for attempt in range(3):
                try:
                    result = await self.api._http.request(route, json=payload)
                    file_info = result.get("file_info", "") if isinstance(result, dict) else getattr(result, "file_info", "")
                    if not file_info:
                        raise RuntimeError(f"{desc}返回空file_info (target={target})")
                    return file_info
                except (OSError, RuntimeError, ConnectionError, TimeoutError) as e:
                    last_err = e
                    if attempt < 2:
                        wait = (attempt + 1) * 3
                        logger.warning("qq_bot.upload_retry", attempt=attempt + 1, wait=wait, error=str(e))
                        await asyncio.sleep(wait)
            raise RuntimeError(f"{desc}失败（已重试3次）") from last_err
        finally:
            if compressed_path is not None:
                try:
                    compressed_path.unlink()
                    logger.info("qq_bot.temp_file_cleaned", path=str(compressed_path))
                except OSError as e:
                    logger.warning(f"qq_bot.temp_file_cleanup_failed: {e}")

    @staticmethod
    def _compress_image(image_path: Path, max_size: int = 800_000, quality: int = 75) -> Path:
        """压缩图片到指定大小以下，返回压缩后的临时文件路径。

        所有中间临时文件会在方法内部清理，只保留最终成功的文件。
        调用者负责在不再需要时删除返回的临时文件。
        """
        from PIL import Image
        import tempfile

        tmp_path: Path | None = None

        try:
            with Image.open(image_path) as img:
                save_img = img.convert("RGB") if img.mode in ("RGBA", "P") else img.copy()

                for q in range(quality, 20, -10):
                    prev_tmp = tmp_path
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                        tmp_path = Path(f.name)
                    save_img.save(tmp_path, "JPEG", quality=q)
                    if prev_tmp is not None:
                        prev_tmp.unlink(missing_ok=True)
                    if tmp_path.stat().st_size <= max_size:
                        logger.info("qq_bot.image_compressed", original=str(image_path),
                                    original_size=image_path.stat().st_size,
                                    compressed_size=tmp_path.stat().st_size, quality=q)
                        return tmp_path

                scale = 0.75
                while scale >= 0.25:
                    new_w = int(save_img.width * scale)
                    new_h = int(save_img.height * scale)
                    resized = save_img.resize((new_w, new_h), Image.LANCZOS)
                    prev_tmp = tmp_path
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                        tmp_path = Path(f.name)
                    resized.save(tmp_path, "JPEG", quality=60)
                    if prev_tmp is not None:
                        prev_tmp.unlink(missing_ok=True)
                    if tmp_path.stat().st_size <= max_size:
                        logger.info("qq_bot.image_resized", original=f"{save_img.width}x{save_img.height}",
                                    resized=f"{new_w}x{new_h}", size=tmp_path.stat().st_size)
                        return tmp_path
                    scale -= 0.1
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

        # 最终兜底：返回最小版本
        return tmp_path

    def _split_text_by_bytes(self, text: str, byte_limit: int = 7800) -> list[str]:
        """按字节上限分割文本，每段不超过 byte_limit 字节。

        P1-6 修复：C2C 流式第 4 段合并后可能远超 QQ API 8000 字节上限，
        合并后需调用本方法按字节再分割，逐片发送。

        - 短文本（≤ byte_limit 字节）返回单片
        - 长文本按字节上限切片，优先在换行处切分
        - 闭合截断处未结束的 markdown 代码块（```）

        Args:
            text: 原始文本
            byte_limit: 字节上限，默认 7800（留 200 字节余量给 8000 字节 QQ API 上限）

        Returns:
            分割后的文本段列表（每段 ≤ byte_limit 字节）
        """
        if not text:
            return []
        encoded = text.encode('utf-8')
        if len(encoded) <= byte_limit:
            return [text]

        from utils.text_utils import _find_char_boundary

        segments: list[str] = []
        remaining = text
        while remaining:
            encoded = remaining.encode('utf-8')
            if len(encoded) <= byte_limit:
                segments.append(remaining)
                break

            # 留 10% 余量避免边界字符是多字节字符
            safe_limit = int(byte_limit * 0.9)
            target_chars = _find_char_boundary(remaining, safe_limit)
            # 优先在换行处切分（向前查找，找到的换行符包含在当前段）
            search_end = min(len(remaining), target_chars + 100)
            best_pos = remaining.rfind('\n', max(0, target_chars - 200), search_end)
            if best_pos == -1 or best_pos < target_chars // 2:
                best_pos = target_chars

            chunk = remaining[:best_pos]
            while len((chunk.rstrip() + ('\n```' if chunk.count('```') % 2 else '')).encode('utf-8')) > byte_limit:
                best_pos -= 1
                chunk = remaining[:best_pos]
            tail = remaining[best_pos:]
            if chunk.count('```') % 2 != 0:
                chunk = chunk.rstrip() + '\n```'
                tail = '```\n' + tail
            remaining = tail
            segments.append(chunk)

        return segments

    def _split_text_for_streaming(self, text: str, chunk_size: int = 300) -> list[str]:
        """将文本切片为流式发送的段。

        - 短回复（< 400 字符）返回单片
        - 长回复按 chunk_size 切片，避免切断 markdown 代码块和 URL
        - chunk_size 默认 300，建议范围 200-400

        Args:
            text: 原始文本
            chunk_size: 每片字符数，默认 300

        Returns:
            切片后的文本段列表
        """
        if not text:
            return []
        if len(text) < 400:
            return [text]

        segments: list[str] = []
        pos = 0
        text_len = len(text)
        while pos < text_len:
            end = min(pos + chunk_size, text_len)
            if end >= text_len:
                segments.append(text[pos:])
                break
            # 调整切片点：避免切断代码块和 URL
            end = self._adjust_boundary_for_code_block(text, pos, end)
            end = self._adjust_boundary_for_url(text, pos, end)
            # 防止零长度段或回退
            if end <= pos:
                end = min(pos + chunk_size, text_len)
            segments.append(text[pos:end])
            pos = end
        return segments

    def _split_group_text(self, text: str) -> list[str]:
        from utils.text_utils import split_for_group_passive

        segments = split_for_group_passive(text)
        if "".join(segments).replace("\n```\n```\n", "\n") == text:
            return segments
        marker = "\n（内容已截断）"
        last = segments[-1].rstrip()
        while len((last + marker).encode("utf-8")) > 4000:
            last = last[:-1]
        segments[-1] = last + marker
        return segments

    def _adjust_boundary_for_code_block(self, text: str, start: int, end: int) -> int:
        """若切片点位于 markdown 代码块内部，向后调整到代码块结束。

        通过统计 [start, end) 范围内的 ``` 数量判断是否在代码块内部。
        若为奇数，表示切片点在代码块内部，需要向后查找下一个 ``` 并调整到其后。

        Args:
            text: 完整文本
            start: 当前段起始位置
            end: 原始切片点

        Returns:
            调整后的切片点
        """
        segment = text[start:end]
        fence_count = segment.count('```')
        if fence_count % 2 == 0:
            return end  # 不在代码块内
        # 在代码块内，找到下一个 ```
        next_fence = text.find('```', end)
        if next_fence == -1:
            return len(text)  # 没有闭合，剩余全部作为一段
        new_end = next_fence + 3
        # 防止单段过大（超过 6000 字符则放弃调整）
        if new_end - start > 6000:
            return end
        return new_end

    def _adjust_boundary_for_url(self, text: str, start: int, end: int) -> int:
        """若切片点位于 URL 中间，向后调整到 URL 结束。

        在 end 之前的窗口内查找最近的 http:// 或 https://，
        若 URL 延伸到 end 之后，则将 end 调整到 URL 结束位置。

        Args:
            text: 完整文本
            start: 当前段起始位置
            end: 原始切片点

        Returns:
            调整后的切片点
        """
        if end >= len(text):
            return end
        # 在 end 之前的窗口内查找最近的 http:// 或 https://
        search_start = max(0, end - 200)
        last_http = text.rfind('http://', search_start, end)
        last_https = text.rfind('https://', search_start, end)
        url_start = max(last_http, last_https)
        if url_start == -1:
            return end
        # URL 结束位置：第一个空白或中英文标点
        url_end = end
        stop_chars = set(' \t\n\r，。；！？「」『』（）()【】[]<>「」')
        while url_end < len(text) and text[url_end] not in stop_chars:
            url_end += 1
        # 防止单段过大
        if url_end - start > 6000:
            return end
        return url_end if url_end > end else end

    @staticmethod
    def _remaining_segments_after_error(segments: list[str], index: int,
                                        exc: BaseException) -> str:
        """发送失败后计算需要合并的剩余文本。

        TimeoutError 时请求可能已发出但响应超时，QQ 服务端可能已接收当前段，
        重发会重复 → 跳过当前段（segments[index+1:]），宁可丢一段也不重复；
        其他异常（连接错误）当前段可能没发出 → 重发含当前段（segments[index:]）。
        """
        if isinstance(exc, TimeoutError):
            return "".join(segments[index + 1:])
        return "".join(segments[index:])

    def _cap_stream_segments(self, segments: list[str], is_group: bool,
                             resplit_event: str, capped_event: str) -> list[str]:
        """按被动回复配额截断流式分片，超出的尾部合并到最后一片。

        C2C 合并后按字节上限重切（避免单条超 QQ API 8000 字节上限）；
        群聊走 split_for_group_passive，每段 ≤4000 字节，最多 4 片不会触发本分支。
        """
        max_segs = QQ_GROUP_MAX_SEGMENTS if is_group else QQ_C2C_MAX_SEGMENTS
        if len(segments) <= max_segs:
            return segments
        original_segment_count = len(segments)
        merged_tail = "".join(segments[max_segs - 1:])
        if not is_group:
            resplit = self._split_text_by_bytes(merged_tail, 7800)
            segments = segments[:max_segs - 1] + resplit
            logger.info(resplit_event + " original={} final={} max_segs={}",
                        original_segment_count, len(segments), max_segs)
        else:
            segments = segments[:max_segs - 1] + [merged_tail]
            logger.info(capped_event + " original={} capped={}",
                        original_segment_count, max_segs)
        return segments

    async def _send_streaming_reply(self, message: Any, full_text: str) -> None:
        """流式分片发送回复，模拟打字效果。

        - 短回复直接发送单片
        - 群聊：按 QQ_GROUP_MSG_BYTE_LIMIT 切片（最多 4 片），全部用 message.reply（被动回复），
          无主动消息降级，不加衔接词。ACK 占 1 次配额，4 片占 4 次，总共 5 次（官方上限）
        - C2C：按 ~300 字符切片，每片间隔 800-1200ms，避免切断代码块/URL
        - 异常时保留已发送片，剩余内容合并为最终片发送

        Args:
            message: QQ Bot 消息对象
            full_text: 完整回复文本
        """
        if not full_text:
            return

        stream_start = time.monotonic()
        total_len = len(full_text)
        is_group = isinstance(message, GroupMessage)
        _group_openid = getattr(message, "group_openid", "") if is_group else ""

        # 群聊：按字节上限切片（最多 4 片，ACK+4片=5次配额）；C2C 按 300 字符切片
        if is_group:
            segments = self._split_group_text(full_text)
        else:
            segments = self._split_text_for_streaming(full_text, chunk_size=300)

        # P0-10: C2C 被动回复最多 4 次，超出部分合并到最后一片
        segments = self._cap_stream_segments(
            segments, is_group,
            "qq_bot.stream_capped_resplit", "qq_bot.stream_capped")

        _group_no_proactive = ("被动回复", "超过限制", "无权限", "40034105")

        async def _send_segment(text: str, *, passive: bool) -> bool:
            """发送单个分片。返回 True 表示真发送成功，False 表示配额耗尽被静默拒绝。

            群聊无主动消息权限，被动超限时不再抛异常而是返回 False，
            让外层循环能合并剩余内容为单条最终消息发送，避免后续段全部丢失。
            """
            try:
                if is_group or passive or not getattr(self, "api", None):
                    await message.reply(content=text, msg_seq=_next_msg_seq())
                else:
                    response = await self.api.post_c2c_message(
                        openid=message.author.user_openid,
                        content=text,
                        msg_type=0,
                        msg_seq=_next_msg_seq(),
                    )
                    if response is None:
                        raise RuntimeError("C2C主动消息接口返回None")
                return True
            except (TimeoutError, OSError, RuntimeError, ValueError) as e:
                err_str = str(e)
                if is_group and any(k in err_str for k in _group_no_proactive):
                    # 群聊被动回复配额超限——静默记录，返回 False 让外层合并剩余
                    logger.info("qq_bot.stream_passive_limited_no_proactive",
                                error=err_str, remaining_to_merge=True)
                    return False
                raise  # 其他异常仍然抛出，由外层异常恢复逻辑处理

        # 短回复：直接发送单片
        if len(segments) <= 1:
            try:
                single = segments[0] if segments else full_text
                t0 = time.monotonic()
                ok = await _send_segment(single, passive=True)
                elapsed = (time.monotonic() - t0) * 1000
                if ok:
                    logger.info("qq_bot.stream_single",
                                total_len=total_len, ms=round(elapsed, 1))
                else:
                    logger.warning("qq_bot.stream_single_quota_exhausted",
                                   total_len=total_len, ms=round(elapsed, 1))
            except (TimeoutError, OSError, RuntimeError) as e:
                logger.error("qq_bot.stream_final_failed", error=str(e))
            return

        num_segments = len(segments)
        logger.info("qq_bot.stream_start", total_len=total_len,
                     segments=num_segments, is_group=is_group)

        # 长回复：首片前发送打字指示（仅 C2C，群聊无主动消息权限会失败）
        if not is_group:
            try:
                await message.reply(content=f"{get_agent_display_name('xiaoda')}正在打字...", msg_seq=_next_msg_seq())
            except (OSError, RuntimeError) as e:
                logger.debug("qq_bot.typing_indicator_failed", error=str(e))

        sent_count = 0
        for i, seg in enumerate(segments):
            try:
                if i > 0:
                    await asyncio.sleep(random.uniform(0.8, 1.2))
                t0 = time.monotonic()
                ok = await _send_segment(seg, passive=i == 0)
                seg_ms = (time.monotonic() - t0) * 1000
                if ok:
                    sent_count += 1
                    logger.debug("qq_bot.stream_segment", index=i, size=len(seg),
                                 ms=round(seg_ms, 1), sent=sent_count)
                else:
                    # 配额耗尽：合并本段 + 剩余所有段为单条最终片发送
                    logger.warning("qq_bot.stream_segment_quota_exhausted",
                                   at_segment=i, sent_segments=sent_count,
                                   total_segments=num_segments)
                    remaining = "".join(segments[i:])
                    # P1-6 修复：合并后按字节上限再分割逐片发送，避免单条超 8000 字节被 QQ API 拒绝
                    # Q4 修复：群聊 recovery 也重切——原合并为单条可能超限，统一按字节重切
                    recovery_pieces = self._split_text_by_bytes(remaining, 7800)
                    for piece in recovery_pieces:
                        try:
                            ok2 = await _send_segment(piece, passive=False)
                            if ok2:
                                sent_count += 1
                            else:
                                logger.error("qq_bot.stream_quota_merge_failed_too",
                                             remaining_len=len(piece))
                                break
                        except (TimeoutError, OSError, RuntimeError) as e2:
                            logger.error("qq_bot.stream_quota_merge_exception",
                                         error=str(e2), remaining_len=len(piece))
                            break
                    if sent_count > 0:
                        logger.info("qq_bot.stream_quota_recovered_with_merge",
                                    merged_from=num_segments - i, sent=sent_count,
                                    ms=round(seg_ms, 1))
                    return
            except (TimeoutError, OSError, RuntimeError) as e:
                logger.warning("qq_bot.stream_segment_failed",
                               error=str(e), sent_segments=sent_count)
                # 异常恢复：合并剩余内容为最终片发送。
                # P0 治本修复（大量文本重复发送根因）：
                #   - TimeoutError：请求已发出但响应超时，QQ 服务端可能已接收当前段，
                #     重发会重复 → 跳过当前段（segments[i+1:]），宁可丢一段也不重复。
                #   - OSError/RuntimeError：连接错误，当前段可能没发出 → 重发含当前段（segments[i:]），
                #     避免丢失。重复只在超时场景发生，连接错误场景不会重复。
                remaining = self._remaining_segments_after_error(segments, i, e)
                # P1-6 修复：合并后按字节上限再分割逐片发送，避免单条超 8000 字节被 QQ API 拒绝
                # Q4 修复：群聊 recovery 也重切——原合并为单条可能超限，统一按字节重切
                recovery_pieces = self._split_text_by_bytes(remaining, 7800)
                recovery_sent = 0
                for piece in recovery_pieces:
                    try:
                        await _send_segment(piece, passive=False)
                        recovery_sent += 1
                    except (TimeoutError, OSError, RuntimeError) as e2:
                        logger.error("qq_bot.stream_final_failed", error=str(e2))
                        break
                if recovery_sent > 0:
                    recovery_ms = (time.monotonic() - stream_start) * 1000
                    logger.info("qq_bot.stream_recovery_done",
                                sent=sent_count + recovery_sent, ms=round(recovery_ms, 1))
                return

        total_ms = (time.monotonic() - stream_start) * 1000
        logger.info("qq_bot.stream_done", total_len=total_len,
                     segments=num_segments, sent=sent_count,
                     ms=round(total_ms, 1))

    async def _send_reply_with_sticker(self, message: Any, result: ProcessResult) -> None:
        reply = result.reply
        clean_reply = self.agent.strip_emotion_tag(reply)

        # 流式输出：长回复且启用环境变量时，分片流式发送
        # 群聊：ACK 占 1 次配额，流式分片最多 4 次配额，总共 5 次（官方上限）
        # C2C：按 300 字符切片
        stream_enabled = os.getenv("QQ_STREAM_REPLY", "true").lower() in ("true", "1", "yes")
        if stream_enabled and len(clean_reply) > 400:
            await self._send_streaming_reply_with_sticker(message, clean_reply, result)
        else:
            await self._send_fallback_reply_with_sticker(message, clean_reply, result)

        # 语音和图片并行发送
        send_tasks = self._gather_media_send_tasks(message, result)

        # 并行等待所有媒体发送完成
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    async def _send_streaming_reply_with_sticker(self, message: Any, clean_reply: str,
                                                   result: ProcessResult) -> None:
        """流式发送长回复，最后一片与表情包合并发送。

        群聊：按 QQ_GROUP_MSG_BYTE_LIMIT 切片（最多 4 片），全部用 message.reply（被动回复），
              不加衔接词。ACK 占 1 次配额，4 片占 4 次，总共 5 次（官方上限）。
        C2C：按 ~300 字符切片，第 1 片被动回复，后续主动消息。
        """
        if not result.sticker_path:
            await self._send_streaming_reply(message, clean_reply)
            return

        is_group = isinstance(message, GroupMessage)
        # 群聊：按字节上限切片（最多 4 片，ACK+4片=5次配额）；C2C：按 300 字符切片
        if is_group:
            segments = self._split_group_text(clean_reply)
        else:
            segments = self._split_text_for_streaming(clean_reply, chunk_size=300)

        # P0-10: C2C 被动回复最多 4 次，超出部分合并到最后一片
        segments = self._cap_stream_segments(
            segments, is_group,
            "qq_bot.stream_sticker_capped_resplit", "qq_bot.stream_sticker_capped")

        if len(segments) <= 1:
            # 短回复：文字+表情包合并为一条消息发送
            try:
                await self._send_reply_with_media(message, clean_reply, image_path=result.sticker_path)
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                await message.reply(content=clean_reply, msg_seq=_next_msg_seq())
            return

        # 长回复：前 N-1 片流式发送，最后一片与表情包合并发送
        _group_openid2 = getattr(message, "group_openid", "") if is_group else ""

        _group_no_proactive_sticker = ("被动回复", "超过限制", "无权限", "40034105")

        async def _send_segment(text: str) -> bool:
            """发送单个分片。返回 True 表示真发送成功，False 表示配额耗尽被静默拒绝。

            与 _send_streaming_reply._send_segment 同构（修复同类 bug）：
            群聊无主动消息权限，被动超限时不再静默吞异常，而是返回 False
            让外层循环能合并剩余内容（含最后一片与 sticker 的合并片），
            避免后续段全部丢失。
            """
            try:
                await message.reply(content=text, msg_seq=_next_msg_seq())
                return True
            except (TimeoutError, OSError, RuntimeError, ValueError) as e:
                err_str = str(e)
                if is_group and any(k in err_str for k in _group_no_proactive_sticker):
                    logger.info("qq_bot.stream_sticker_passive_limited_no_proactive",
                                error=err_str, remaining_to_merge=True)
                    return False
                raise  # 其他异常仍然抛出，由外层异常恢复逻辑处理

        # 发送前 N-1 片
        for i, seg in enumerate(segments[:-1]):
            try:
                if i > 0:
                    await asyncio.sleep(random.uniform(0.8, 1.2))
                t0 = time.monotonic()
                ok = await _send_segment(seg)
                seg_ms = (time.monotonic() - t0) * 1000
                if ok:
                    logger.debug("qq_bot.stream_sticker_segment",
                                 index=i, size=len(seg), ms=round(seg_ms, 1))
                else:
                    # 配额耗尽：合并剩余所有段（含最后一片）为单条最终片，
                    # 与 sticker 合并发送（msg_type=7 支持图文混排）
                    logger.warning("qq_bot.stream_sticker_segment_quota_exhausted",
                                   at_segment=i, total_segments=len(segments))
                    remaining = "".join(segments[i:])
                    try:
                        await self._send_reply_with_media(
                            message, remaining, image_path=result.sticker_path)
                        logger.info("qq_bot.stream_sticker_quota_recovered_with_merge",
                                    merged_from=len(segments) - i, ms=round(seg_ms, 1))
                    except (OSError, RuntimeError, ConnectionError) as e2:
                        logger.error("qq_bot.stream_sticker_quota_merge_failed",
                                     error=str(e2), remaining_len=len(remaining))
                        # 最终兜底：放弃 sticker，仅发送文本
                        try:
                            await message.reply(content=remaining, msg_seq=_next_msg_seq())
                        except (TimeoutError, OSError, RuntimeError) as e3:
                            logger.error("qq_bot.stream_sticker_fallback_failed",
                                         error=str(e3))
                    return
            except (TimeoutError, OSError, RuntimeError) as e:
                logger.warning("qq_bot.stream_sticker_segment_failed", error=str(e))
                # 异常恢复：合并剩余内容（含最后一片）与 sticker 一起发送
                remaining = self._remaining_segments_after_error(segments, i, e)
                if not remaining:
                    return
                try:
                    pieces = self._split_text_by_bytes(remaining, 7800)
                    for piece in pieces[:-1]:
                        await _send_segment(piece)
                    await self._send_reply_with_media(
                        message, pieces[-1], image_path=result.sticker_path)
                    logger.info("qq_bot.stream_sticker_recovery_done_with_merge")
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error("qq_bot.stream_sticker_recovery_failed", error=str(e2))
                    # 兜底：放弃 sticker，仅发送合并文本
                    try:
                        for piece in self._split_text_by_bytes(remaining, 7800):
                            await _send_segment(piece)
                    except (TimeoutError, OSError, RuntimeError) as e3:
                        logger.error("qq_bot.stream_sticker_recovery_final_failed",
                                     error=str(e3))
                return

        # 最后一片与表情包合并发送（msg_type=7 支持图文混排）
        last_seg = segments[-1]
        try:
            await self._send_reply_with_media(message, last_seg, image_path=result.sticker_path)
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.warning("qq_bot.sticker_with_last_segment_failed", error=str(e))
            try:
                # 兜底：放弃 sticker，仅发送最后一片文本
                ok = await _send_segment(last_seg)
                if not ok:
                    logger.error("qq_bot.sticker_last_segment_quota_exhausted_no_recovery")
            except (OSError, RuntimeError) as e2:
                logger.debug("qq_bot.fallback_segment_also_failed", error=str(e2))

    async def _send_fallback_reply_with_sticker(self, message: Any, clean_reply: str,
                                                  result: ProcessResult) -> None:
        """短回复或流式禁用时，单条发送回复+表情包。

        群聊场景：短回复（<=400字符）直接发送；流式禁用时超长按 split_for_group_passive
        取第 1 片（无标记截断，自动闭合代码块）。
        C2C 场景：保持原分片逻辑。
        """
        from utils.text_utils import split_long_reply, split_for_group_passive

        is_group = isinstance(message, GroupMessage)

        if is_group:
            # 群聊：用 split_for_group_passive 切片，逐条发送（不只发第 1 片）
            # P1-5 修复：原版本仅发 segments[0]，segments[1..] 静默丢弃且无截断标记。
            # 现在遍历全部 segments 逐条发送；若超过 QQ 群 ACK 配额（4 段），
            # 只发前 4 段，第 4 段末尾追加 "\n（…）" 提示用户内容被截断。
            original_len = len(clean_reply.encode('utf-8'))
            segments = split_for_group_passive(clean_reply)
            group_quota = QQ_GROUP_MAX_SEGMENTS  # ACK 占 1 次，被动回复最多 4 次
            if len(segments) > group_quota:
                # 超过配额：只发前 4 段，第 4 段末尾追加截断标记
                segments = segments[:group_quota]
                segments[-1] = segments[-1].rstrip() + "\n（…）"
                logger.info("qq_bot.group_reply_quota_truncated_with_marker",
                            original_bytes=original_len,
                            sent_segments=group_quota,
                            marker="（…）")
            # 逐条发送所有 segments；最后一段赋给 final_text 走下方 sticker 合并发送路径
            sent_count = 0
            for i, seg in enumerate(segments[:-1]):
                try:
                    await message.reply(content=seg, msg_seq=_next_msg_seq())
                    sent_count += 1
                except (OSError, RuntimeError, ConnectionError) as e:
                    logger.warning("qq_bot.group_reply_part_failed",
                                   part_index=i, total_parts=len(segments), error=str(e))
                    # 失败时合并剩余段为单条发送（避免静默丢失）。
                    # P0 治本修复（重复发送根因，与流式路径同构）：
                    #   TimeoutError 跳过当前段（可能已发，避免重复）；
                    #   其他异常重发含当前段（可能没发，避免丢失）。
                    remaining = self._remaining_segments_after_error(segments, i, e)
                    try:
                        await message.reply(content=remaining, msg_seq=_next_msg_seq())
                        sent_count += 1
                        logger.info("qq_bot.group_reply_merge_recovered",
                                    merged_from=len(segments) - i)
                        final_text = ""  # 已全部发完
                    except (OSError, RuntimeError, ConnectionError) as e2:
                        logger.error("qq_bot.group_reply_merge_failed",
                                     error=str(e2), remaining_len=len(remaining))
                        final_text = segments[-1] + "\n（内容过长部分发送失败）"
                    break
            else:
                # 循环正常结束：前 N-1 段全部发送成功，最后一段走 sticker 合并发送
                final_text = segments[-1]
            truncated_len = sum(len(s.encode('utf-8')) for s in segments)
            if truncated_len < original_len and not (len(segments) == group_quota):
                logger.info("qq_bot.group_reply_truncated_no_marker",
                            original_bytes=original_len, truncated_bytes=truncated_len,
                            dropped_segments=0)
        else:
            # C2C：保持原分片逻辑
            parts = split_long_reply(clean_reply, MAX_REPLY_LEN)
            if len(parts) == 1:
                final_text = parts[0]
            else:
                # 与 _send_streaming_reply._send_segment 同构的修复：
                # 某段发送失败时（含配额超限），合并剩余所有段（含最后一段）为单条发送，
                # 而非 break+加错误提示（旧版本会导致中间所有段无声丢失，用户只看到最后一段+错误提示）。
                # 合并成功后 final_text="" 表示已全部发完，仅发送 sticker；
                # 合并也失败时退化为原行为（发最后一段+错误提示）。
                final_text = parts[-1]
                merge_done = False
                for i, part in enumerate(parts[:-1]):
                    try:
                        await message.reply(content=part, msg_seq=_next_msg_seq())
                    except (OSError, RuntimeError, ConnectionError) as e:
                        logger.warning("qq_bot.long_reply_part_failed_merging",
                                       part_index=i, total_parts=len(parts), error=str(e))
                        # 合并剩余段为单条发送。
                        # P0 治本修复（重复发送根因，与流式/群聊路径同构）：
                        #   TimeoutError 跳过当前段（可能已发，避免重复）；
                        #   其他异常重发含当前段（可能没发，避免丢失）。
                        remaining = self._remaining_segments_after_error(parts, i, e)
                        try:
                            for piece in self._split_text_by_bytes(remaining, 7800):
                                await message.reply(content=piece, msg_seq=_next_msg_seq())
                            logger.info("qq_bot.long_reply_merge_recovered",
                                        merged_from=len(parts) - i)
                            merge_done = True
                            final_text = ""  # 已全部发完，仅保留 sticker
                        except (OSError, RuntimeError, ConnectionError) as e2:
                            logger.error("qq_bot.long_reply_merge_failed",
                                         error=str(e2), remaining_len=len(remaining))
                            # 最终兜底：在最后一片加错误提示
                            final_text = parts[-1] + "\n（内容过长部分发送失败）"
                        break  # 无论合并成功或失败，都退出循环
                if not merge_done and final_text == parts[-1]:
                    # 循环正常结束（未触发 break），所有前段发送成功，发最后一段
                    final_text = parts[-1]

        # 1. 文字+表情包立刻发送（用户最快看到回复）
        # 合并成功时 final_text="" 表示文字已全部发完，跳过本步骤（避免发空消息）
        if result.sticker_path:
            try:
                await self._send_reply_with_media(message, final_text, image_path=result.sticker_path)
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                try:
                    await message.reply(content=final_text, msg_seq=_next_msg_seq())
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error("qq_bot.sticker_fallback_reply_failed", error=str(e2))
        elif final_text:
            # 仅当还有内容未发送时才发（避免合并成功后发空消息）
            try:
                await message.reply(content=final_text, msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error("qq_bot.final_text_reply_failed", error=str(e))

    def _gather_media_send_tasks(self, message: Any, result: ProcessResult) -> list:
        """构建媒体发送任务列表（TTS 语音/视频/图片），用于并行发送。"""
        send_tasks = []

        # TTS 语音发送（同步模式：audio_path 已有缓存文件）
        if result.audio_path and result.audio_path.exists():
            async def _send_cached_audio() -> None:
                try:
                    await self._send_audio(message, result.audio_path)
                except (OSError, RuntimeError, ConnectionError) as e:
                    logger.warning("qq_bot.audio_send_failed", error=str(e))
            send_tasks.append(_send_cached_audio())

        # TTS 语音发送（异步模式：tts_pending=True 时现场合成）
        elif getattr(result, "tts_pending", False) and result.tts_text:
            async def _send_async_tts() -> None:
                try:
                    audio_path = await self.agent.tts.synthesize_xiaoda(
                        result.tts_text, emotion=result.emotion or ""
                    )
                    if audio_path and audio_path.exists():
                        await self._send_audio(message, audio_path)
                    else:
                        logger.warning("qq_bot.async_tts_no_audio")
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("qq_bot.async_tts_failed", error=str(e))
            send_tasks.append(_send_async_tts())

        # 视频发送
        if result.video_path and result.video_path.exists():
            async def _send_vid() -> None:
                try:
                    await self._send_video(message, result.video_path)
                except (OSError, RuntimeError, ConnectionError) as e:
                    logger.warning("qq_bot.video_send_failed", error=str(e))
            send_tasks.append(_send_vid())

        # 图片发送
        if result.image_paths:
            async def _send_images() -> None:
                image_paths = result.image_paths[:QQ_GROUP_MEDIA_BUDGET] if isinstance(message, GroupMessage) else result.image_paths
                for img_path in image_paths:
                    try:
                        await self._send_reply_with_media(message, "", image_path=img_path)
                    except (OSError, RuntimeError, ConnectionError) as e:
                        logger.error("qq_bot.image_send_error", error=str(e), path=str(img_path))
                        try:
                            await message.reply(content="图片生成成功，但发送失败", msg_seq=_next_msg_seq())
                        except (OSError, RuntimeError, ConnectionError) as e2:
                            logger.error(f"qq_bot.image_fallback_reply_failed: {e2}")
            send_tasks.append(_send_images())

        return send_tasks

    async def _send_video(self, message: Any, video_path: Path) -> None:
        """发送视频消息"""
        try:
            if isinstance(message, C2CMessage):
                file_info = await self._upload_c2c_base64(message.author.user_openid, video_path, file_type=2)
                await self.api.post_c2c_message(
                    openid=message.author.user_openid,
                    msg_type=7,
                    content="",
                    media={"file_info": file_info},
                    msg_seq=_next_msg_seq(),
                    msg_id=message.id
                )
            elif isinstance(message, GroupMessage):
                file_info = await self._upload_group_base64(message.group_openid, video_path, file_type=2)
                try:
                    await self.api.post_group_message(
                        group_openid=message.group_openid,
                        msg_type=7,
                        content="",
                        media={"file_info": file_info},
                        msg_seq=_next_msg_seq(),
                        msg_id=message.id
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.video_passive_limited_switching_to_proactive")
                        await self.api.post_group_message(
                            group_openid=message.group_openid,
                            msg_type=7,
                            content="",
                            media={"file_info": file_info},
                            msg_seq=_next_msg_seq(),
                        )
                    else:
                        raise
            logger.info("qq_bot.video_sent", video_path=str(video_path))
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.error("qq_bot.video_send_error", error=str(e), video_path=str(video_path))
            # 降级为文本消息
            try:
                await message.reply(content=f"视频生成成功，但发送失败: {e}", msg_seq=_next_msg_seq())
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error(f"qq_bot.video_fallback_reply_failed: {e}")

    async def _send_audio(self, message: Any, audio_path: Path) -> None:
        silk_path = None
        try:
            silk_path = await self._convert_to_silk(audio_path)
            if silk_path is None:
                logger.warning("qq_bot.silk_convert_failed", path=str(audio_path))
                await message.reply(content="语音消息发送失败：缺少 SILK 编码库，请联系管理员安装 pilk", msg_seq=_next_msg_seq())
                return

            if isinstance(message, C2CMessage):
                openid = message.author.user_openid
                file_info = await self._upload_c2c_base64(openid, silk_path, file_type=3)
                await self.api.post_c2c_message(
                    openid=openid, msg_id=message.id,
                    msg_type=7, content="",
                    media={"file_info": file_info}, msg_seq=_next_msg_seq()
                )
            elif isinstance(message, GroupMessage):
                group_openid = message.group_openid
                file_info = await self._upload_group_base64(group_openid, silk_path, file_type=3)
                try:
                    await self.api.post_group_message(
                        group_openid=group_openid, msg_id=message.id,
                        msg_type=7, content="",
                        media={"file_info": file_info}, msg_seq=_next_msg_seq()
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.audio_passive_limited_switching_to_proactive")
                        await self.api.post_group_message(
                            group_openid=group_openid,
                            msg_type=7, content="",
                            media={"file_info": file_info}, msg_seq=_next_msg_seq()
                        )
                    else:
                        raise
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.warning("qq_bot.audio_send_error", error=str(e))
        finally:
            # 只清理中间文件（silk），不删除输入文件（audio_path）
            if silk_path is not None:
                try:
                    p = Path(silk_path)
                    if p.exists():
                        p.unlink()
                        logger.info("qq_bot.temp_file_cleaned", path=str(p))
                except (OSError, RuntimeError) as e:
                    logger.warning(f"qq_bot.audio_temp_cleanup_failed: {e}")

    async def _convert_to_silk(self, audio_path: Path) -> Path | None:
        pcm_path = None
        silk_path = None
        converted = False
        try:
            import pilk

            pcm_path = audio_path.with_suffix('.pcm')
            silk_path = audio_path.with_suffix('.silk')

            def _do_convert() -> bool:
                result = subprocess.run(
                    ['ffmpeg', '-y', '-i', str(audio_path), '-ar', '16000', '-ac', '1', '-f', 's16le', str(pcm_path)],
                    capture_output=True, text=True, timeout=30, check=False
                )
                if result.returncode != 0:
                    logger.warning("qq_bot.ffmpeg_failed", stderr=result.stderr[:200])
                    return False
                pilk.encode(str(pcm_path), str(silk_path), pcm_rate=16000, tencent=True)
                return True

            ok = await asyncio.to_thread(_do_convert)

            if ok and silk_path.exists() and silk_path.stat().st_size > 0:
                converted = True
                logger.info("qq_bot.silk_convert_ok", input=str(audio_path), output=str(silk_path),
                            size_kb=silk_path.stat().st_size // 1024)
                return silk_path
            return None
        except ImportError:
            logger.warning("qq_bot.pilk_not_installed")
            return None
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            logger.warning("qq_bot.silk_convert_failed", error=str(e))
            return None
        finally:
            if pcm_path is not None:
                pcm_path.unlink(missing_ok=True)
            if silk_path is not None and not converted:
                silk_path.unlink(missing_ok=True)


if __name__ == "__main__":
    # P0 修复：实时从 env 读取（与 run_qq_bot 保持一致，防止模块级变量未更新）
    _main_app_id = os.getenv("QQBOT_APP_ID", "").strip() or APP_ID
    _main_app_secret = os.getenv("QQBOT_APP_SECRET", "").strip() or APP_SECRET
    if not _main_app_id or _main_app_id == "your_app_id_here":
        print("=" * 55)
        print("  请先配置 QQ Bot AppID 和 AppSecret")
        print("")
        print("  步骤:")
        print("  1. 浏览器打开: https://q.qq.com")
        print("  2. 用手机 QQ 扫码登录")
        print("  3. 点击「创建机器人」")
        print("  4. 复制 AppID 和 AppSecret")
        print("  5. 填入 .env 文件")
        print("=" * 55)
        sys.exit(1)

    print("=" * 50)
    print(f"{get_agent_display_name('xiaoda')}的 QQ Bot 启动中...")
    print("  私聊: 全自动回复")
    print("  群聊: @机器人 触发")
    print("=" * 50)

    intents = botpy.Intents(public_messages=True)
    is_sandbox = _qq_cfg.get("is_sandbox", False)

    MAX_RETRIES = 100
    BASE_DELAY = 5
    MAX_DELAY = 120

    retry_count = 0

    while retry_count < MAX_RETRIES:
        try:
            client = AIQQBot(intents=intents, is_sandbox=is_sandbox, timeout=30)
            # 每次重连都用最新的 env 值（与 run_qq_bot 保持一致）
            _main_app_id = os.getenv("QQBOT_APP_ID", "").strip() or APP_ID
            _main_app_secret = os.getenv("QQBOT_APP_SECRET", "").strip() or APP_SECRET
            client.run(appid=_main_app_id, secret=_main_app_secret)
            retry_count = 0
            logger.warning("qq_bot.exited_normally_restarting")
        except KeyboardInterrupt:
            logger.info("qq_bot.keyboard_interrupt")
            break
        except (TimeoutError, OSError, RuntimeError, ConnectionError) as e:
            retry_count += 1
            delay = min(BASE_DELAY * (2 ** min(retry_count - 1, 6)), MAX_DELAY)
            logger.error(
                "qq_bot.crashed_retrying",
                error=str(e)[:200],
                retry=retry_count,
                delay=delay,
            )
            print(f"\n  ⚠ QQ Bot 异常退出: {str(e)[:100]}")
            print(f"  🔄 {delay:.0f} 秒后重连 (第 {retry_count} 次)...\n")
            # __main__ 块为同步上下文（client.run 是同步调用），使用 time.sleep
            time.sleep(delay)

    if retry_count >= MAX_RETRIES:
        logger.error("qq_bot.max_retries_exceeded")
        print("  ❌ QQ Bot 重连次数已达上限，退出")
        sys.exit(1)
