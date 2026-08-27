import asyncio
import base64
import contextvars
import hashlib
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from dotenv import load_dotenv

from channel_adapter_base import (
    STREAM_C2C_MAX_SEGMENTS,
    STREAM_GROUP_MAX_SEGMENTS,
    ChannelAdapterBase,
    CoreProcessRequest,
    parse_env_csv,
    upsert_env_file_line,
)

# P0 修复（Windows 安装包 QQ 离线 bug 根因）：
# load_dotenv() 无参数时只读取 CWD/.env，Windows 安装包从 C:\Program Files\ 启动时
# CWD 不是用户目录，而 config.py 已把 .env 放到 ~/.ai-agent/.env（frozen 模式），
# 导致 APP_ID/APP_SECRET 永远为空 → run_qq_bot 早期返回 disabled_no_appid → QQ 离线。
# 修复：显式使用 config.ENV_PATH，与 config.py 保持一致。
try:
    from config import ENV_PATH as _ENV_PATH
    # override=False 与 config_paths/agent.py 统一：进程环境优先，.env 只补缺
    # （P0 修复的要点是显式 ENV_PATH 路径解析，与 override 策略无关）
    load_dotenv(_ENV_PATH, override=False)
except ImportError:
    # config 模块不可用时兜底（如独立运行模式），退化为无参数 load_dotenv
    load_dotenv()


from utils.common import safe_float as _safe_float
from utils.common import safe_int as _safe_int
from utils.llm_cleanup import strip_qq_face_tags
from utils.logging_config import setup_logging
from utils.thread_pools import to_thread_heavy

setup_logging()

import botpy  # noqa: E402
from botpy.message import C2CMessage, GroupMessage  # noqa: E402
from loguru import logger  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_core import AgentCore, ProcessResult  # noqa: E402
from agent_core._shared import (  # noqa: E402
    _group_context_enabled_var,
    _group_context_metadata_var,
    is_degraded_reply,
)
from agent_core.group_context import (  # noqa: E402
    GroupContextRegistry,
    GroupSnapshot,
    format_group_snapshot,
)
from agent_core.user_qq import QQUser  # noqa: E402
from botpy_compat import install_botpy_patches, redirect_bot_log  # noqa: E402
from config import (  # noqa: E402
    AGENT_CONFIG,
    GROUP_CHAT_BUFFER_ENABLED,
    get_agent_display_name,
)
from config_constants import env_flag  # noqa: E402
from core.background_tasks import (  # noqa: E402
    reset_current_request_context,
    set_current_request_context,
)
from core.event_bus import event_bus  # noqa: E402
from emotion.emoji_config import get_ack_message  # noqa: E402
from emotion.nudge_engine import NudgeEngine  # noqa: E402
from security.human_approval import (  # noqa: E402
    HIGH_RISK_OPERATIONS,
    ApprovalRequest,
    ApprovalStatus,
    IMApprovalChannel,
    RiskLevel,
)
from utils.text_utils import encode_image_to_base64  # noqa: E402

install_botpy_patches()
APP_ID = os.getenv("QQBOT_APP_ID", "")
APP_SECRET = os.getenv("QQBOT_APP_SECRET", "")

_qq_cfg = AGENT_CONFIG.get("qq_bot", {})
MAX_REPLY_LEN = _qq_cfg.get("max_reply_length", 8000)
# A2：分片配额常量已下沉到 channel_adapter_base（STREAM_*_MAX_SEGMENTS），
# 这里保留同名别名，维持既有调用点与测试的 import 面不变。
QQ_C2C_MAX_SEGMENTS = STREAM_C2C_MAX_SEGMENTS
QQ_GROUP_MAX_SEGMENTS = STREAM_GROUP_MAX_SEGMENTS
QQ_GROUP_MEDIA_BUDGET = 3

# HITL: Agent 输出中嵌入的高危操作标记，QQ 适配器拦截后触发两段式确认
_HIGH_RISK_OP_MARKER = "__HIGH_RISK_OP__:"
_HIGH_RISK_OP_RE = re.compile(
    r"__HIGH_RISK_OP__:\s*(\w+)(?:\s+(.*))?\s*$", re.MULTILINE)

_msg_seq_counter = int(time.time())
_msg_seq_lock = threading.Lock()
_env_write_lock = threading.Lock()


class QQReplyBudgetExceeded(RuntimeError):
    """The current QQ group request exhausted its five-message allowance."""


@dataclass
class QQReplyBudget:
    max_total: int = 5
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_total - self.used)

    def consume(self) -> None:
        if self.used >= self.max_total:
            raise QQReplyBudgetExceeded("QQ group reply budget exhausted")
        self.used += 1

    def refund(self) -> None:
        if self.used > 0:
            self.used -= 1


_qq_reply_budget_var: contextvars.ContextVar[QQReplyBudget | None] = (
    contextvars.ContextVar("qq_reply_budget", default=None)
)


def _refresh_runtime_owner_id(owner_id: str) -> None:
    """绑定主人后，刷新当前进程运行时 SecurityFilter 的主人集合，使识别即时生效。

    延迟导入避免循环依赖（security → core → adapter 链）。core 单例不可用时
    静默跳过（下次重启仍会从 .env 加载，不影响正确性）。
    """
    try:
        from web.server import app as _web_app
        core = getattr(_web_app, "state", None) and getattr(_web_app.state, "core", None)
        if core is not None and getattr(core, "security", None) is not None:
            core.security.add_owner_id(owner_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("qq_bot.refresh_runtime_owner_failed error={}", str(e)[:120])


# QQ API 要求 msg_seq 为 int32 范围（0~2147483647）。实测毫秒时间戳（13 位，
# ~1.7e12）超范围被拒（40011000「请求数据异常」），导致所有回复发送失败。
# 改用秒级时间戳（10 位，2038-01-19 前安全），仍保证：1) 单调递增；
# 2) 时钟回拨/进程休眠后计数器落后时不产生回退，避免服务端拒绝。


def _next_msg_seq() -> int:
    budget = _qq_reply_budget_var.get()
    if budget is not None:
        budget.consume()
    global _msg_seq_counter
    with _msg_seq_lock:
        now_s = int(time.time())
        _msg_seq_counter = max(_msg_seq_counter + 1, now_s)
        return _msg_seq_counter


class QQAmbiguousDelivery(RuntimeError):
    """平台返回 None：发送结果不明确（可能已送达）。

    恢复路径必须按 TimeoutError 同款语义处理——跳过当前段宁丢勿重，
    不得把该段并入重发文本（否则实际已送达时用户收到重复消息）。
    """


async def _budgeted_await(
    factory: Callable[[int], Awaitable[Any]],
    *,
    none_is_failure: bool = True,
) -> Any:
    """Run one QQ send and refund its budget when delivery clearly fails."""
    budget = _qq_reply_budget_var.get()
    msg_seq = _next_msg_seq()
    try:
        result = await factory(msg_seq)
    except BaseException:
        if budget is not None:
            budget.refund()
        raise
    if result is False or (none_is_failure and result is None):
        if budget is not None:
            budget.refund()
        if result is None:
            # None 是"结果不明确"而非明确失败：配额照退（不能基于猜测补发配额），
            # 但异常类型单独标记，供恢复路径跳过当前段防重复。
            raise QQAmbiguousDelivery("QQ platform send returned no success result")
        raise RuntimeError("QQ platform send returned no success result")
    return result


async def _budgeted_reply(message: Any, content: str, **kwargs: Any) -> Any:
    return await _budgeted_await(
        lambda msg_seq: message.reply(content=content, msg_seq=msg_seq, **kwargs),
        none_is_failure=False,
    )


def _save_master_openid(openid: str) -> None:
    """将 openid 追加到 MASTER_QQ_OPENID（逗号分隔），并更新运行时环境变量。

    绑定后同步刷新运行时 SecurityFilter.owner_ids，使主人识别即时生效
    （无需重启服务）。
    """
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
        upsert_env_file_line(env_path, "MASTER_QQ_OPENID", value)
        os.environ["MASTER_QQ_OPENID"] = value
        logger.info("qq_bot.master_openid_saved", openid=openid, total=len(ids))
        # 运行时即时生效：刷新当前进程 SecurityFilter 的主人集合
        _refresh_runtime_owner_id(openid)


def _parse_master_ids() -> list[str]:
    """解析 MASTER_QQ_OPENID 环境变量为去空白的 openid 列表（逗号分隔）。"""
    return parse_env_csv("MASTER_QQ_OPENID")


def _build_user_input(content: str, attachment_info: str) -> str:
    """拼接文本与附件描述为用户输入。"""
    return f"{content} {attachment_info}".strip() if content else attachment_info


# 当前活跃的 bot 实例（同进程内 GreetingScheduler 等主动消息入口使用）
_ACTIVE_BOT: "AIQQBot | None" = None


def get_active_bot() -> "AIQQBot | None":
    """返回当前活跃的 QQ bot 实例；web 层读取一律经此访问，勿直读 _ACTIVE_BOT。"""
    return _ACTIVE_BOT


async def send_proactive_message(text: str, openid: str = "",
                                 sticker_path: Path | str | None = None) -> bool:
    """向最近私聊用户（或指定 openid）主动发一条 QQ 消息。

    可选携带表情包：先上传图片再与正文合并为一条图文消息（msg_type=7，与
    主对话 _send_reply_with_sticker 同一发送语义）；上传/发送失败自动降级为
    纯文本（msg_type=0），不中断整个主动问候/提醒投递。

    供 web/greeting_scheduler、emotion/nudge_engine 等同进程模块调用；
    QQ client 未连接时返回 False。
    """
    bot = _ACTIVE_BOT
    if bot is None or bot.is_closed():
        raise RuntimeError("QQ client 未连接")
    target = openid or bot._last_c2c_openid
    if not target:
        raise RuntimeError("没有可用的 QQ 用户 openid（等用户先发一条私聊，或设置 NUDGE_USER_OPENID）")
    _sticker = Path(sticker_path) if sticker_path else None
    if _sticker is not None and _sticker.exists():
        try:
            file_info = await bot._upload_c2c_base64(target, _sticker)
            await bot.api.post_c2c_message(
                openid=target, msg_type=7, content=text,
                media={"file_info": file_info}, msg_seq=_next_msg_seq())
            logger.info("qq_bot.proactive_sent_with_sticker openid={} text={} sticker={}",
                        target[:8], text[:40], _sticker.name)
            return True
        except (OSError, RuntimeError, ConnectionError, TimeoutError) as e:
            # 表情包上传/合成失败不阻塞问候本身，降级为纯文本继续发送
            logger.warning("qq_bot.proactive_sticker_fallback error={}", str(e)[:120])
    await bot.api.post_c2c_message(
        openid=target, content=text, msg_type=0, msg_seq=_next_msg_seq())
    logger.info("qq_bot.proactive_sent openid={} text={}", target[:8], text[:40])
    return True


async def run_qq_bot(agent: "AgentCore", *, sandbox: bool = False) -> None:
    """在现有事件循环中运行 QQ client（与 WebUI 同进程模式）。

    内部带指数退避重连；任务被取消时干净退出。
    T2：每次迭代结束（正常断开 / 异常重连 / 取消）都经 client.close() 回收
    当前实例——close() 会停止 on_ready 启动的 nudge engine 并关闭 SDK 连接，
    保证凭证轮换重启时旧实例（含周期任务）全部被回收，不再泄漏。
    """
    redirect_bot_log()
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
        finally:
            # T2：无论正常断开还是异常重连，离开本次迭代前统一回收当前实例
            # （幂等；CancelledError 分支已 close 过则此处为空操作）。
            try:
                await client.close()
            except (OSError, RuntimeError, AttributeError) as e:
                logger.warning(
                    "qq_bot.reclaim_instance_failed error={}", str(e)[:200],
                )


class ParsedC2CMessage(NamedTuple):
    """C2C 消息解析结果（原 5 元组改为命名结构，提升可读性）。"""
    content: str
    image_data: list
    user_input: str
    user_openid: str
    user_id: str


class AttachmentResult(NamedTuple):
    """附件处理结果（原 2 元组改为命名结构）。"""
    image_data: list
    attachment_info: str


@dataclass
class QQPipelineRequest(CoreProcessRequest):
    """QQ 管道请求：在骨架请求上补充消息对象与 C2C/群聊差异字段。

    C2C 与群聊的全部行为差异以 ``is_group`` 为单一事实源派生：
    发送器（_bus_reply_fn）、ACK 容错、EventBus notify_started、错误日志
    exc_info、session_id 形态（C2C=真实 DB 会话 ID，群聊=qq_group:{群 openid}
    合成边界，core 侧再拼装为 qq_group:{openid}:{group}）——禁止在钩子之外
    散落通道判别。
    """

    message: Any = None       # botpy 消息对象（C2CMessage / GroupMessage）
    is_group: bool = False    # False=C2C，True=群聊
    image_data: Any = None
    is_master: bool = True
    group_context_enabled: bool = False
    system_context: str = ""
    group_context_metadata: dict[str, Any] | None = None
    user_context_token: Any = None

    @property
    def channel_key(self) -> str:
        """/whoami 与日志事件名片段："c2c" / "group"。"""
        return "group" if self.is_group else "c2c"


class AIQQBot(ChannelAdapterBase, botpy.Client):
    """QQ 机器人适配器，处理消息接收、去重与 AgentCore 调用。"""
    def __init__(self, *args: Any, agent: "AgentCore | None" = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 支持注入共享的 AgentCore（与 WebUI 同进程同实例），未注入则自建（独立运行模式）
        self.agent = agent or AgentCore()
        self._agent_shared = agent is not None
        self.nudge_engine = None
        # T2：close()/stop() 幂等标志（botpy Client._closed 只覆盖 ws 连接，
        # 不覆盖 nudge engine 等适配器层资源）
        self._adapter_closed = False
        # 消息去重缓存：msg_id → 时间戳，保留最近 1 小时（见 ChannelAdapterBase）
        self._init_dedup_state()
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
        self._group_context_registry = GroupContextRegistry()
        self._c2c_session_cache_ttl = 3600  # 缓存有效期 1 小时
        self._c2c_session_cache_ts: dict[str, float] = {}
        # P1-1: 缓存上限，超过时按 FIFO 淘汰最旧条目（防多用户长期运行内存泄漏）
        self._C2C_SESSION_CACHE_MAX_SIZE = 1000
        # HITL: 高危操作两段式确认（默认开启，QQ_HITL_ENABLED=false 关闭）
        self.hitl_enabled = env_flag("QQ_HITL_ENABLED", True)
        self.im_approval = IMApprovalChannel(
            send_callback=self._send_approval_message,
            timeout=_safe_float(os.getenv("QQ_HITL_TIMEOUT", "60"), 60),
        )
        self._approval_message_ctx = self._new_approval_context()
        global _ACTIVE_BOT
        _ACTIVE_BOT = self

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
        """P1-1: 清理 C2C session 缓存中的过期与超限条目（实现在基类）。"""
        self._session_cache_prune(
            self._c2c_session_cache, self._c2c_session_cache_ts,
            ttl=self._c2c_session_cache_ttl,
            max_size=self._C2C_SESSION_CACHE_MAX_SIZE)

    def _invalidate_c2c_session(self, user_openid: str) -> None:
        """P1-2: 主动失效指定用户的 session_id 缓存。

        场景: agent.process 抛错（session 失效、被删除等）时调用，
        保证下次消息重新查 DB 获取最新 session_id。
        """
        self._c2c_session_cache.pop(user_openid, None)
        self._c2c_session_cache_ts.pop(user_openid, None)

    def _set_c2c_session_cache(self, user_openid: str, sid: str) -> None:
        """CodeRabbit F8: 统一缓存写入 + 立即执行 size cap（实现在基类）。"""
        self._session_cache_set(
            self._c2c_session_cache, self._c2c_session_cache_ts,
            user_openid, sid,
            ttl=self._c2c_session_cache_ttl,
            max_size=self._C2C_SESSION_CACHE_MAX_SIZE)

    @staticmethod
    def _get_config_service() -> Any:
        try:
            from core_runtime.config_service import get_config_service
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

        nudge_enabled = env_flag("NUDGE_ENABLED", False)
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
                        from core_runtime.node_registry import get_backend, get_local_model
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

    async def close(self) -> None:
        """统一生命周期回收入口（T2）：停止 nudge engine + 关闭 SDK client。

        幂等：重复调用安全。重连循环（run_qq_bot）每次迭代结束、凭证轮换重启、
        graceful shutdown 都必须经此回收旧实例，避免 on_ready 启动的 nudge
        周期任务随实例被丢弃后永久泄漏（旧实现全仓无 stop 调用）。
        """
        await self._reclaim_adapter_resources()

    async def stop(self) -> None:
        """:meth:`close` 的别名——语义对齐 WeChatBotAdapter.stop()。"""
        await self.close()

    async def _reclaim_adapter_resources(self) -> None:
        """实际回收逻辑；幂等由 _adapter_closed 标志保证。"""
        if getattr(self, "_adapter_closed", False):
            return
        self._adapter_closed = True
        # 1. 停止 nudge engine（NudgeEngine.stop 自身幂等：重复调用/未 start 均安全）
        nudge = getattr(self, "nudge_engine", None)
        if nudge is not None:
            try:
                await nudge.stop()
            except Exception as e:  # noqa: BLE001 —— 回收失败不阻断 client 关闭
                logger.warning("qq_bot.nudge_stop_failed error={}", str(e)[:200])
            self.nudge_engine = None
        # 2. 关闭 SDK client（botpy Client.close 自带 _closed 幂等保护）
        try:
            await super().close()
        except (OSError, RuntimeError, AttributeError) as e:
            logger.warning("qq_bot.client_close_failed error={}", str(e)[:200])

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
            await _budgeted_reply(msg, text)
        except QQReplyBudgetExceeded:
            logger.info("qq_bot.approval_budget_exhausted")
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

    @staticmethod
    def _attachment_ok_part(ct: str, fn: str, result: dict) -> tuple[str, dict | None]:
        """附件接收成功时返回 (描述文本, 图片数据或 None)。"""
        if result.get("text_preview"):
            return f"[文件: {fn}]\n内容预览:\n{result['text_preview'][:500]}", None
        if ct.startswith("image/"):
            save_path = result.get('save_path', '')
            part = f"[图片: {fn}，已保存到 {save_path}]"
            try:
                mime, img_b64 = encode_image_to_base64(save_path)
                return part, {"mimeType": mime, "data": img_b64}
            except FileNotFoundError:
                return part, None
            except (OSError, ValueError, RuntimeError) as e:
                logger.warning("qq_bot.image_encode_failed", error=str(e))
                return part, None
        return f"[文件: {fn}，已保存到 {result['save_path']}]", None

    @staticmethod
    def _attachment_failed_part(ct: str, fn: str) -> str:
        """附件接收失败时的描述文本。"""
        if ct.startswith("image/"):
            return f"[图片: {fn or 'image'}]"
        if ct.startswith("video/"):
            return f"[视频: {fn or 'video'}]"
        return f"[附件: {fn or 'unknown'}]"

    async def _process_message_attachments(self, message: Any) -> AttachmentResult:
        """处理消息中的附件，返回图片数据和附件描述文本。

        遍历消息附件，接收文件、编码图片为 base64，生成附件描述文本。
        C2C 消息和群消息的附件处理逻辑完全一致，提取此方法消除重复。

        Args:
            message: QQ Bot 消息对象（C2CMessage 或 GroupMessage）

        Returns:
            AttachmentResult(image_data, attachment_info)
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
                    part, img = self._attachment_ok_part(ct, fn, result)
                    parts.append(part)
                    if img is not None:
                        image_data.append(img)
                else:
                    parts.append(self._attachment_failed_part(ct, fn))
            attachment_info = " ".join(str(p) for p in parts)
        return AttachmentResult(image_data, attachment_info)

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

        if await self._handle_quick_commands(content, message, user_openid, user_id):
            return

        # 并发处理消息（per-user 锁保证同一用户串行，不同用户并发）
        if user_openid not in self._c2c_locks:
            self._c2c_locks[user_openid] = asyncio.Lock()

        async def _c2c_reply_with_lock() -> None:
            try:
                async with self._c2c_locks[user_openid]:
                    session_id = await self._get_or_create_c2c_session(user_openid)
                    await self._run_message_pipeline(
                        message, is_group=False,
                        user_input=user_input, user_id=user_id, openid=user_openid,
                        is_master=is_master, image_data=image_data,
                        session_id=session_id)
            finally:
                self._cleanup_message_lock(self._c2c_locks, user_openid)

        # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致回复丢失，
        # 改 _spawn（跟踪引用 + 完成回收），保证用户一定收到回复。
        from core.background_tasks import _spawn
        _spawn(_c2c_reply_with_lock())

    def _extract_c2c_sender(self, message: C2CMessage) -> tuple[str, str]:
        """提取 C2C 发送者 openid 与规范化 user_id，并缓存最近 openid。"""
        user_openid = getattr(message.author, 'user_openid', '') if hasattr(message, 'author') else ''
        user_id = f"qq_{user_openid}" if user_openid else "qq_unknown"
        if user_openid:
            self._last_c2c_openid = user_openid
        return user_openid, user_id

    async def _parse_c2c_message(self, message: C2CMessage) -> ParsedC2CMessage | None:
        """解析 C2C 消息内容和发送者信息。

        返回 ParsedC2CMessage(content, image_data, user_input, user_openid, user_id)，
        若消息为空（无文本且无附件）返回 None。
        """
        content = (getattr(message, 'content', None) or "").strip()
        content = strip_qq_face_tags(content)  # 剥离 QQ 表情标签，防止污染 LLM 上下文被模仿
        image_data, attachment_info = await self._process_message_attachments(message)
        if not content and not attachment_info:
            return None
        user_input = _build_user_input(content, attachment_info)

        user_openid, user_id = self._extract_c2c_sender(message)
        logger.info("qq_bot.c2c_message", user_id=user_id, openid=user_openid, content=user_input[:80])
        return ParsedC2CMessage(content, image_data, user_input, user_openid, user_id)

    def _identify_c2c_master(self, user_openid: str) -> bool:
        """识别发送者是否为主人。

        安全策略：不再"首个私聊者自动绑主"。若 MASTER_QQ_OPENID 未配置，
        所有私聊用户均视为非主人（fail-closed），并通过 /whoami 引导用户
        在 Setup Wizard 中显式录入主人 openid。这避免公开部署时任意第一个
        私聊者窃取主人权限。
        """
        master_ids = _parse_master_ids()
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
        统一实现在 ChannelAdapterBase._get_or_create_session_cached（微信侧同源）。
        """
        return await self._get_or_create_session_cached(
            user_openid,
            core=self.agent,
            cache=self._c2c_session_cache,
            cache_ts=self._c2c_session_cache_ts,
            ttl=self._c2c_session_cache_ttl,
            max_size=self._C2C_SESSION_CACHE_MAX_SIZE,
            tmp_prefix="qq_tmp_",
            log_prefix="qq_bot",
            event_stem="c2c_session",
        )

    async def _handle_quick_commands(self, content: str, message: Any,
                                     openid: str, user_id: str) -> bool:
        """处理 C2C/群聊共用的快捷指令（原两侧各一份的合并体）。

        - /whoami 指令：回复发送者的 openid（用于主人在 Setup 中填写）
        - HITL: 若用户有待审批请求，先尝试匹配回复（"确认"/"取消"），
          匹配则跳过正常处理
        返回 True 表示已处理，跳过正常流程。
        """
        if content.strip() in ("/whoami", "/whoami "):
            await _budgeted_reply(
                message,
                f"你的 OpenID 是：\n{openid}\n\n在 Setup 配置页面的「主人 QQ OpenID」填入此值即可绑定主人身份。",
            )
            return True
        if self.hitl_enabled:
            approval_user = openid or user_id
            if await self.im_approval.handle_user_reply(approval_user, content):
                return True
        return False

    # ------------------------------------------------------------------
    # C2C / 群聊共享回复管道（模板方法 + 骨架钩子）
    # 原 _process_c2c_reply 与 _run_group_agent/_send_group_ack 两侧平行实现
    # 的沉淀层；差异经 QQPipelineRequest.is_group 收敛，文案逐字节保持不变。
    # ------------------------------------------------------------------

    async def _run_message_pipeline(self, message: Any, *, is_group: bool,
                                    user_input: str, user_id: str, openid: str,
                                    is_master: bool, image_data: Any,
                                    session_id: str | None = None,
                                    group_key: str = "") -> ProcessResult | None:
        """C2C/群聊统一回复管道（模板方法）。

        覆盖原两侧逐字复制的区段：ACK → 绑定 EventBus 用户 →
        wait_for(agent.process, 120s) → 高危审批（HITL）→ sticker/媒体回复 →
        超时与异常兜底。通道差异经 req.is_group 派生的钩子消化；
        调用方（on_c2c_message_create / _handle_group_at_message）保留各自的
        解析/去重/锁时序，保证消息去重键与 HITL 触发条件不变。
        """
        if is_group:
            real_group_openid = str(getattr(message, "group_openid", "") or "")
            if not real_group_openid:
                raise ValueError("QQ group message missing group_openid")
            group_key = real_group_openid
            session_id = f"qq_group:{real_group_openid}"

        system_context = ""
        group_context_metadata = None
        group_buffer = None
        snapshot: GroupSnapshot | None = None
        group_context_enabled = bool(
            is_group and group_key and GROUP_CHAT_BUFFER_ENABLED
        )
        if is_group and group_key:
            group_context_metadata = {
                "chat_type": "qq_group",
                "group_key": hashlib.sha256(group_key.encode("utf-8")).hexdigest(),
                "actor_alias": "群成员",
                "is_owner": is_master,
                "message_id": (
                    getattr(message, "id", "")
                    or getattr(message, "message_id", "")
                ),
            }
        if group_context_enabled:
            group_buffer = await self._group_context_registry.get(group_key)
            current, snapshot = await group_buffer.append_and_snapshot(
                message_id=(getattr(message, "id", "") or getattr(message, "message_id", "")),
                member_id=openid,
                role="user",
                content=user_input,
                observed_at=time.time(),
            )
            system_context = format_group_snapshot(snapshot)
            group_context_metadata = {
                "chat_type": "qq_group",
                "group_key": hashlib.sha256(group_key.encode("utf-8")).hexdigest(),
                "actor_alias": current.actor_alias,
                "is_owner": is_master,
                "message_id": current.message_id,
            }

        req = QQPipelineRequest(
            text=user_input,
            user_id=user_id,
            source="qq_group" if is_group else "qq_c2c",
            user_openid=openid,
            session_id=session_id,
            image_data=image_data if image_data else None,
            message=message,
            is_group=is_group,
            is_master=is_master,
            group_context_enabled=group_context_enabled,
            system_context=system_context,
            group_context_metadata=group_context_metadata,
        )
        budget_token = _qq_reply_budget_var.set(
            QQReplyBudget() if is_group else None
        )
        enabled_token = _group_context_enabled_var.set(group_context_enabled)
        metadata_token = _group_context_metadata_var.set(group_context_metadata)
        audit_token = set_current_request_context(group_context_metadata or {})
        try:
            result = await self._process_with_core(req)
        except BaseException:
            if group_buffer is not None and snapshot is not None:
                await group_buffer.commit_failure(snapshot)
            raise
        finally:
            reset_current_request_context(audit_token)
            _group_context_metadata_var.reset(metadata_token)
            _group_context_enabled_var.reset(enabled_token)
            _qq_reply_budget_var.reset(budget_token)
        if group_buffer is not None and snapshot is not None:
            if result is None or not result.reply or is_degraded_reply(result.reply):
                await group_buffer.commit_failure(snapshot)
            else:
                await group_buffer.commit_success(
                    snapshot,
                    message_id=f"assistant:{snapshot.through_seq}",
                    content=result.reply,
                    observed_at=time.time(),
                )
        return result

    async def _process_c2c_reply(self, message: C2CMessage, user_input: str, user_id: str,
                                  user_openid: str, session_id: str, is_master: bool,
                                  image_data: list) -> None:
        """兼容入口（旧签名保留供测试/外部调用）：转调统一管道模板。"""
        await self._run_message_pipeline(
            message, is_group=False,
            user_input=user_input, user_id=user_id, openid=user_openid,
            is_master=is_master, image_data=image_data, session_id=session_id)

    # -- ChannelAdapterBase._process_with_core 骨架的 QQ 侧钩子 --

    def _get_core(self) -> Any:
        return self.agent

    def _build_process_kwargs(
        self,
        req: QQPipelineRequest,
        session_id: str | None,
    ) -> dict[str, Any]:
        kwargs = super()._build_process_kwargs(req, session_id)
        kwargs["image_data"] = req.image_data
        kwargs["is_master"] = req.is_master
        kwargs["user_context_token_callback"] = (
            lambda token: setattr(req, "user_context_token", token)
        )
        if req.group_context_enabled:
            kwargs["system_context"] = req.system_context
        return kwargs

    async def _send_ack(self, req: QQPipelineRequest) -> None:
        """处理前 ACK。C2C 失败上抛走骨架错误兜底（原行为）；
        群聊容忍失败只记 debug——群聊被动回复配额 5 次/5 分钟，ACK 失败不应
        再消耗兜底配额（原 _send_group_ack 行为）。"""
        if not req.is_group:
            await _budgeted_await(
                lambda msg_seq: req.message.reply(
                    content=get_ack_message('xiaoda'), msg_seq=msg_seq,
                ),
            )
            return
        try:
            await _budgeted_await(
                lambda msg_seq: req.message.reply(
                    content=get_ack_message('xiaoda'), msg_seq=msg_seq,
                ),
            )
        except QQReplyBudgetExceeded:
            logger.info("qq_bot.ack_budget_exhausted")
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.debug("qq_bot.ack_send_failed", error=str(e))

    def _make_status_callback(self, req: QQPipelineRequest) -> Any:
        async def status_notify(msg) -> None:
            # 所有中间状态消息（工具状态、进度提示等）不发送到 QQ
            # 实际回复通过 _send_reply_with_sticker / _send_streaming_reply 发送
            return
        return status_notify

    def _bus_reply_fn(self, req: QQPipelineRequest):
        """EventBus 中间通知的发送器：C2C 走主动消息 API（None 校验），群聊走被动 reply。"""
        if not req.is_group:
            openid = req.user_openid

            async def _qq_reply(content: str, msg_seq: int = 0) -> None:
                response = await self.api.post_c2c_message(
                    openid=openid,
                    content=content,
                    msg_type=0,
                    msg_seq=msg_seq,
                )
                if response is None:
                    raise RuntimeError("C2C状态消息接口返回None")
            return _qq_reply

        message = req.message

        async def _group_reply(content: str, msg_seq: int = 0) -> None:
            await message.reply(content=content, msg_seq=msg_seq)
        return _group_reply

    def _bind_bus_user(self, req: QQPipelineRequest) -> Any:
        # Q1 修复：群聊被动回复配额 5 次/5 分钟，ACK+4 片回复已占满 5 次，
        # SUB_STARTED 通知会击穿配额触发 40034105 → 群聊禁用开始通知（notify_started=False）。
        qq_user = QQUser(reply_fn=self._bus_reply_fn(req), msg_seq_fn=_next_msg_seq,
                         notify_started=not req.is_group)
        return event_bus.bind_user(qq_user)

    def _unbind_bus_user(self, token: Any) -> None:
        if token is not None:
            event_bus.unbind_user(token)

    async def _post_process_result(self, req: QQPipelineRequest, result: ProcessResult) -> ProcessResult:
        """高危操作两段式确认 + sticker 回复（须在骨架保护区内执行，
        异常才能落入错误兜底文案——与原 try 块包裹范围一致）。"""
        # HITL: 高危操作两段式确认（检测 __HIGH_RISK_OP__ 标记）
        result = await self._check_high_risk_approval(
            result, req.message, req.user_openid or req.user_id, req.is_master)
        if result.reply:
            await self._send_reply_with_sticker(req.message, result)
        return result

    async def _pipeline_timeout_fallback(
        self,
        channel_key: str,
        message: Any,
        user_id: str,
        user_input: str,
        user_context_token: Any = None,
    ) -> None:
        """超时兜底（C2C/群聊共用）：记录失败状态 + 发送超时文案（原文案逐字节保留）。"""
        logger.warning(f"qq_bot.{channel_key}_timeout user=%s", user_id)
        # 记录失败状态，供下次消息恢复上下文
        if hasattr(self.agent, 'context') and self.agent.context:
            await self.agent.context.record_failure(
                user_context_token,
                "处理超时",
                user_input,
            )
        try:
            await _budgeted_reply(
                message,
                f"{get_agent_display_name('xiaoda')}想得太入神了……能再说一次吗？🌱",
            )
        except QQReplyBudgetExceeded:
            logger.info(f"qq_bot.{channel_key}_timeout_budget_exhausted")
        except (OSError, RuntimeError, ConnectionError) as _e:
            logger.debug(f"qq_bot.{channel_key}_timeout_reply_failed", error=str(_e))

    async def _pipeline_error_fallback(self, channel_key: str, message: Any, exc: BaseException,
                                       *, exc_info: bool = False, openid: str = "",
                                       invalidate_session: bool = False) -> None:
        """异常兜底（C2C/群聊共用）：日志 + 可选会话失效 + 发送错误文案（原文案逐字节保留）。"""
        logger.error(f"qq_bot.{channel_key}_error: {exc}", exc_info=exc_info)
        # P1-2: 仅在 agent 处理失败（非 QQ 网络短暂错误）时失效 session 缓存
        # RuntimeError/OSError 可能是 QQ 网络短暂错误（ACK/回复发送失败），不应清除健康缓存
        # ValueError 通常表示数据格式问题（如 session 无效），需要重新查 DB
        if invalidate_session and openid and isinstance(exc, ValueError):
            self._invalidate_c2c_session(openid)
        try:
            await _budgeted_reply(message, "嗯……出了点小问题，等会儿再聊好不好？")
        except QQReplyBudgetExceeded:
            logger.info(f"qq_bot.{channel_key}_fallback_budget_exhausted")
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.error(f"qq_bot.{channel_key}_fallback_reply_failed: {e}")

    async def _on_core_timeout(self, req: QQPipelineRequest) -> None:
        key = req.channel_key
        await self._pipeline_timeout_fallback(
            key,
            req.message,
            req.user_id,
            req.text,
            req.user_context_token,
        )

    async def _on_core_error(self, req: QQPipelineRequest, exc: BaseException) -> None:
        await self._pipeline_error_fallback(
            req.channel_key, req.message, exc,
            exc_info=req.is_group,
            openid=req.user_openid,
            # 仅 C2C 有会话缓存可失效（原实现群聊分支无此逻辑）
            invalidate_session=not req.is_group,
        )

    async def _extract_group_message_input(self, message: GroupMessage) -> tuple[str, Any, Any, str, str]:
        """提取群消息输入：返回 (content, image_data, attachment_info, member_openid, user_id)。"""
        content = (getattr(message, 'content', None) or "").strip()
        content = strip_qq_face_tags(content)  # 剥离 QQ 表情标签，防止污染 LLM 上下文被模仿
        image_data, attachment_info = await self._process_message_attachments(message)
        member_openid = getattr(message.author, 'member_openid', '') if hasattr(message, 'author') else ''
        user_id = f"qq_{member_openid}" if member_openid else "qq_unknown"
        return content, image_data, attachment_info, member_openid, user_id

    def _identify_group_master(self, member_openid: str) -> bool:
        """识别群消息发送者是否为主人（C2C 判定回调的群聊变体）。

        对比 member_openid 与 MASTER_QQ_OPENID（逗号分隔多值）；
        on_group_add_robot 已自动绑定拉群者的 member_openid。
        """
        master_ids = _parse_master_ids()
        is_master = bool(master_ids) and member_openid in master_ids
        if is_master:
            logger.info("qq_bot.master_identified", member_openid=member_openid)
        return is_master

    async def _handle_group_at_message(self, message: GroupMessage, group_lock_key: str) -> None:
        # Q5 修复：user_id/user_input 定义于 try 块内，_process_message_attachments
        # 等步骤抛异常时 TimeoutError/error 分支引用它们会 NameError——
        # 在方法入口预置默认值，保证异常分支可安全引用（对齐 c2c 的参数绑定）。
        user_id = "qq_unknown"
        user_input = ""
        try:
            async with self._group_locks[group_lock_key]:
                content, image_data, attachment_info, member_openid, user_id = \
                    await self._extract_group_message_input(message)

                if not content and not attachment_info:
                    return

                user_input = _build_user_input(content, attachment_info)
                logger.info("qq_bot.group_message", user_id=user_id, openid=member_openid, content=user_input[:80])

                is_master = self._identify_group_master(member_openid)
                if not is_master:
                    logger.info("qq_bot.non_master_message", user_id=user_id, openid=member_openid, content=user_input[:80])

                if self.nudge_engine:
                    self.nudge_engine.poke()

                if await self._handle_quick_commands(content, message, member_openid, user_id):
                    return

                await self._run_message_pipeline(
                    message, is_group=True,
                    user_input=user_input, user_id=user_id, openid=member_openid,
                    is_master=is_master, image_data=image_data,
                    # 群边界 session_id：core 侧 _init_and_restore_context 会拼成
                    # qq_group:{member_openid}:{group_openid}，实现跨群/跨用户上下文隔离
                    session_id=f"qq_group:{group_lock_key}",
                    group_key=group_lock_key)
        except TimeoutError:
            # 解析/鉴权阶段的超时兜底（process 阶段由骨架 _on_core_timeout 兜底，
            # 两者共用同一实现避免文案漂移）
            await self._pipeline_timeout_fallback("group", message, user_id, user_input)
        except (RuntimeError, OSError, ValueError) as e:
            await self._pipeline_error_fallback("group", message, e, exc_info=True)
        finally:
            self._cleanup_message_lock(self._group_locks, group_lock_key)

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        msg_id = getattr(message, 'id', '') or getattr(message, 'message_id', '')
        if msg_id and self._is_duplicate_msg(msg_id):
            return
        _group_lock_key = getattr(message, 'group_openid', '')
        if not _group_lock_key:
            _group_lock_key = "qq_unknown"
        if _group_lock_key not in self._group_locks:
            self._group_locks[_group_lock_key] = asyncio.Lock()

        # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致回复丢失。
        from core.background_tasks import _spawn
        _spawn(self._handle_group_at_message(message, _group_lock_key))

    async def _send_reply_with_media(self, message: Any, reply: str,
                                      image_path: Path | None = None,
                                      image_url: str | None = None) -> None:
        if not image_path and not image_url:
            await _budgeted_reply(message, reply)
            return

        try:
            if isinstance(message, C2CMessage):
                await self._send_c2c_media(message, reply, image_path, image_url)
            elif isinstance(message, GroupMessage):
                await self._send_group_media(message, reply, image_path, image_url)
            else:
                await _budgeted_reply(message, reply)
        except QQReplyBudgetExceeded:
            logger.info("qq_bot.media_budget_exhausted")
            return
        except (OSError, RuntimeError, ConnectionError, ValueError) as e:
            logger.warning("qq_bot.media_send_failed", error=str(e))
            # 最终兜底：尝试纯文本回复
            try:
                await _budgeted_reply(message, reply)
            except QQReplyBudgetExceeded:
                logger.info("qq_bot.media_fallback_budget_exhausted")
            except (OSError, RuntimeError, ConnectionError) as _e:
                logger.debug("qq_bot.fallback_reply_failed", error=str(_e))

    async def _send_c2c_media(self, message: Any, reply: str,
                               image_path: Path | None, image_url: str | None) -> None:
        """C2C 媒体回复：上传 base64/URL 文件后 post_c2c_message。失败抛异常由调用方兜底。"""
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
        response = await _budgeted_await(
            lambda msg_seq: self.api.post_c2c_message(
                openid=openid, msg_id=message.id,
                msg_type=7, content=reply,
                media={"file_info": file_info}, msg_seq=msg_seq,
            ),
        )
        if response is None:
            raise RuntimeError("C2C消息接口返回None")

    async def _send_group_media(self, message: Any, reply: str,
                                 image_path: Path | None, image_url: str | None) -> None:
        """群媒体回复：上传 base64/URL 文件后 post_group_message。

        被动回复超限时记录后跳过（无主动消息权限，不再降级）；其它异常上抛由调用方兜底。
        """
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
            await _budgeted_await(
                lambda msg_seq: self.api.post_group_message(
                    group_openid=group_openid, msg_id=message.id,
                    msg_type=7, content=reply,
                    media={"file_info": file_info}, msg_seq=msg_seq,
                ),
            )
        except (OSError, RuntimeError, ConnectionError) as e:
            if "被动回复" in str(e) or "超过限制" in str(e):
                # 被动回复超限，无主动消息权限，记录后跳过（不再降级为主动消息）
                logger.warning("qq_bot.group_media_passive_limited_no_proactive",
                               error=str(e))
            else:
                raise

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

            file_data = await to_thread_heavy(_read)
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
        import tempfile

        from PIL import Image

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

    @staticmethod
    def _remaining_segments_after_error(segments: list[str], index: int,
                                        exc: BaseException) -> str:
        """发送失败后计算需要合并的剩余文本。

        TimeoutError / QQAmbiguousDelivery 时请求可能已发出（响应超时或平台
        返回 None），QQ 服务端可能已接收当前段，重发会重复 → 跳过当前段
        （segments[index+1:]），宁可丢一段也不重复；
        其他异常（连接错误）当前段可能没发出 → 重发含当前段（segments[index:]）。
        """
        if isinstance(exc, (TimeoutError, QQAmbiguousDelivery)):
            return "".join(segments[index + 1:])
        return "".join(segments[index:])

    async def _send_remaining_segments(self, remaining: str, *, send_piece, log_key: str) -> tuple:
        """失败恢复统一内核：剩余文本按字节上限（7800）重切后逐片发送。

        P0 治本 / P1-6 / Q4 系列修复的唯一实现（原先在流式配额、流式异常、
        sticker 兜底、C2C 长回复四处逐字复制——任何一处漏改都会重新引入
        "大量文本重复发送/静默丢失"P0）：
          - 合并后按字节上限重切，避免单条超限被 QQ API 拒绝；
          - 任一片失败（异常，或群聊被动配额拒绝返回 False）即停止，后续片放弃；
        返回 (成功片数, 总片数)。调用方按需判断部分成功。
        sticker 图文合并（msg_type=7）与群聊单条合并因传输语义不同，不走本内核。
        """
        pieces = self._split_text_by_bytes(remaining, 7800)
        sent = 0
        for piece in pieces:
            try:
                ok = await send_piece(piece)
            except (OSError, RuntimeError) as e:
                # OSError 已涵盖 TimeoutError/ConnectionError 子类
                logger.error(log_key + ".merge_send_failed",
                             error=str(e), remaining_len=len(piece))
                break
            if ok is False:
                logger.error(log_key + ".merge_quota_exhausted",
                             remaining_len=len(piece))
                break
            sent += 1
        return sent, len(pieces)

    async def _send_stream_segment(self, message: Any, text: str, *,
                                   passive: bool, is_group: bool, log_key: str) -> bool:
        """发送单个流式分片。True=发送成功，False=群聊被动配额耗尽被静默拒绝。

        纯文本流式与 sticker 流式共用：群聊无主动消息权限，被动超限时返回
        False 让外层合并剩余内容为最终片，避免后续段全部丢失（修复同类 bug）。
        """
        group_no_proactive = ("被动回复", "超过限制", "无权限", "40034105")
        try:
            if is_group or passive or not getattr(self, "api", None):
                await _budgeted_await(
                    lambda msg_seq: message.reply(content=text, msg_seq=msg_seq),
                )
            else:
                await _budgeted_await(
                    lambda msg_seq: self.api.post_c2c_message(
                        openid=message.author.user_openid,
                        content=text,
                        msg_type=0,
                        msg_seq=msg_seq,
                    ),
                )
            return True
        except QQReplyBudgetExceeded:
            logger.info(f"{log_key}_budget_exhausted")
            raise
        except (TimeoutError, OSError, RuntimeError, ValueError) as e:
            err_str = str(e)
            if is_group and any(k in err_str for k in group_no_proactive):
                logger.info(f"{log_key}_passive_limited_no_proactive",
                            error=err_str, remaining_to_merge=True)
                return False
            raise

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

        # 短回复：直接发送单片
        if len(segments) <= 1:
            try:
                single = segments[0] if segments else full_text
                t0 = time.monotonic()
                ok = await self._send_stream_segment(message, single, passive=True, is_group=is_group, log_key="qq_bot.stream")
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
                await _budgeted_reply(
                    message, f"{get_agent_display_name('xiaoda')}正在打字...",
                )
            except QQReplyBudgetExceeded:
                logger.info("qq_bot.typing_indicator_budget_exhausted")
            except (OSError, RuntimeError) as e:
                logger.debug("qq_bot.typing_indicator_failed", error=str(e))

        sent_count = 0

        async def _send_one(segment: str) -> bool:
            nonlocal sent_count
            index = sent_count
            ok = await self._send_stream_segment(
                message,
                segment,
                passive=index == 0,
                is_group=is_group,
                log_key="qq_bot.stream",
            )
            if ok:
                sent_count += 1
            return ok

        async def _recover(index: int, failure: Any) -> None:
            nonlocal sent_count
            if isinstance(failure, QQReplyBudgetExceeded):
                logger.info(
                    "qq_bot.stream_budget_exhausted",
                    at_segment=index,
                    sent_segments=sent_count,
                    total_segments=num_segments,
                )
                return
            if failure is False:
                logger.warning(
                    "qq_bot.stream_segment_quota_exhausted",
                    at_segment=index,
                    sent_segments=sent_count,
                    total_segments=num_segments,
                )
                remaining = "".join(segments[index:])
            else:
                logger.warning(
                    "qq_bot.stream_segment_failed",
                    error=str(failure),
                    sent_segments=sent_count,
                )
                remaining = self._remaining_segments_after_error(
                    segments,
                    index,
                    failure,
                )
            merged_sent, _ = await self._send_remaining_segments(
                remaining,
                send_piece=lambda piece: self._send_stream_segment(
                    message,
                    piece,
                    passive=False,
                    is_group=is_group,
                    log_key="qq_bot.stream",
                ),
                log_key="qq_bot.stream",
            )
            sent_count += merged_sent
            if merged_sent > 0:
                logger.info(
                    "qq_bot.stream_recovery_done",
                    sent=sent_count,
                    ms=round((time.monotonic() - stream_start) * 1000, 1),
                )

        all_sent = await self._send_segments_paced(
            segments,
            _send_one,
            on_failure=_recover,
            log_prefix="qq_bot",
        )
        if not all_sent:
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
            except QQReplyBudgetExceeded:
                logger.info("qq_bot.sticker_budget_exhausted")
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.warning("qq_bot.sticker_send_failed", error=str(e))
                await _budgeted_reply(message, clean_reply)
            return

        # 长回复：前 N-1 片流式发送，最后一片与表情包合并发送
        _group_openid2 = getattr(message, "group_openid", "") if is_group else ""

        # 发送前 N-1 片
        for i, seg in enumerate(segments[:-1]):
            try:
                if i > 0:
                    await asyncio.sleep(random.uniform(0.8, 1.2))
                t0 = time.monotonic()
                ok = await self._send_stream_segment(message, seg, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
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
                            await _budgeted_reply(message, remaining)
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
                        await self._send_stream_segment(message, piece, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
                    await self._send_reply_with_media(
                        message, pieces[-1], image_path=result.sticker_path)
                    logger.info("qq_bot.stream_sticker_recovery_done_with_merge")
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error("qq_bot.stream_sticker_recovery_failed", error=str(e2))
                    # 兜底：放弃 sticker，仅发送合并文本
                    await self._send_remaining_segments(
                        remaining,
                        send_piece=lambda p: self._send_stream_segment(
                            message, p, passive=True, is_group=is_group,
                            log_key="qq_bot.stream_sticker"),
                        log_key="qq_bot.stream_sticker")
                return

        # 最后一片与表情包合并发送（msg_type=7 支持图文混排）
        last_seg = segments[-1]
        try:
            await self._send_reply_with_media(message, last_seg, image_path=result.sticker_path)
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.warning("qq_bot.sticker_with_last_segment_failed", error=str(e))
            try:
                # 兜底：放弃 sticker，仅发送最后一片文本
                ok = await self._send_stream_segment(message, last_seg, passive=True, is_group=is_group, log_key="qq_bot.stream_sticker")
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
        from utils.text_utils import split_for_group_passive, split_long_reply

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
                    await _budgeted_reply(message, seg)
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
                        await _budgeted_reply(message, remaining)
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
                        await _budgeted_reply(message, part)
                    except (OSError, RuntimeError, ConnectionError) as e:
                        logger.warning("qq_bot.long_reply_part_failed_merging",
                                       part_index=i, total_parts=len(parts), error=str(e))
                        # 合并剩余段为单条发送。
                        # P0 治本修复（重复发送根因，与流式/群聊路径同构）：
                        #   TimeoutError 跳过当前段（可能已发，避免重复）；
                        #   其他异常重发含当前段（可能没发，避免丢失）。
                        remaining = self._remaining_segments_after_error(parts, i, e)
                        merged_sent, merged_total = await self._send_remaining_segments(
                            remaining,
                            send_piece=lambda p: _budgeted_reply(message, p),
                            log_key="qq_bot.long_reply")
                        if merged_sent == merged_total:
                            logger.info("qq_bot.long_reply_merge_recovered",
                                        merged_from=len(parts) - i)
                            merge_done = True
                            final_text = ""  # 已全部发完，仅保留 sticker
                        else:
                            logger.error("qq_bot.long_reply_merge_failed",
                                         remaining_len=len(remaining))
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
                    await _budgeted_reply(message, final_text)
                except (OSError, RuntimeError, ConnectionError) as e2:
                    logger.error("qq_bot.sticker_fallback_reply_failed", error=str(e2))
        elif final_text:
            # 仅当还有内容未发送时才发（避免合并成功后发空消息）
            try:
                await _budgeted_reply(message, final_text)
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
                            await _budgeted_reply(
                                message, "图片生成成功，但发送失败",
                            )
                        except (OSError, RuntimeError, ConnectionError) as e2:
                            logger.error(f"qq_bot.image_fallback_reply_failed: {e2}")
            send_tasks.append(_send_images())

        return send_tasks

    async def _send_video(self, message: Any, video_path: Path) -> None:
        """发送视频消息"""
        try:
            if isinstance(message, C2CMessage):
                file_info = await self._upload_c2c_base64(message.author.user_openid, video_path, file_type=2)
                await _budgeted_await(
                    lambda msg_seq: self.api.post_c2c_message(
                        openid=message.author.user_openid,
                        msg_type=7,
                        content="",
                        media={"file_info": file_info},
                        msg_seq=msg_seq,
                        msg_id=message.id,
                    ),
                )
            elif isinstance(message, GroupMessage):
                file_info = await self._upload_group_base64(message.group_openid, video_path, file_type=2)
                try:
                    await _budgeted_await(
                        lambda msg_seq: self.api.post_group_message(
                            group_openid=message.group_openid,
                            msg_type=7,
                            content="",
                            media={"file_info": file_info},
                            msg_seq=msg_seq,
                            msg_id=message.id,
                        ),
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.video_passive_limited_switching_to_proactive")
                        await _budgeted_await(
                            lambda msg_seq: self.api.post_group_message(
                                group_openid=message.group_openid,
                                msg_type=7,
                                content="",
                                media={"file_info": file_info},
                                msg_seq=msg_seq,
                            ),
                        )
                    else:
                        raise
            logger.info("qq_bot.video_sent", video_path=str(video_path))
        except QQReplyBudgetExceeded:
            logger.info("qq_bot.video_budget_exhausted")
        except (OSError, RuntimeError, ConnectionError) as e:
            logger.error("qq_bot.video_send_error", error=str(e), video_path=str(video_path))
            # 降级为文本消息
            try:
                await _budgeted_reply(
                    message, f"视频生成成功，但发送失败: {e}",
                )
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error(f"qq_bot.video_fallback_reply_failed: {e}")

    async def _send_audio(self, message: Any, audio_path: Path) -> None:
        silk_path = None
        try:
            silk_path = await self._convert_to_silk(audio_path)
            if silk_path is None:
                logger.warning("qq_bot.silk_convert_failed", path=str(audio_path))
                await _budgeted_reply(
                    message,
                    "语音消息发送失败：缺少 SILK 编码库，请联系管理员安装 pilk",
                )
                return

            if isinstance(message, C2CMessage):
                openid = message.author.user_openid
                file_info = await self._upload_c2c_base64(openid, silk_path, file_type=3)
                await _budgeted_await(
                    lambda msg_seq: self.api.post_c2c_message(
                        openid=openid,
                        msg_id=message.id,
                        msg_type=7,
                        content="",
                        media={"file_info": file_info},
                        msg_seq=msg_seq,
                    ),
                )
            elif isinstance(message, GroupMessage):
                group_openid = message.group_openid
                file_info = await self._upload_group_base64(group_openid, silk_path, file_type=3)
                try:
                    await _budgeted_await(
                        lambda msg_seq: self.api.post_group_message(
                            group_openid=group_openid,
                            msg_id=message.id,
                            msg_type=7,
                            content="",
                            media={"file_info": file_info},
                            msg_seq=msg_seq,
                        ),
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as e:
                    if "被动回复" in str(e) or "超过限制" in str(e):
                        logger.info("qq_bot.audio_passive_limited_switching_to_proactive")
                        await _budgeted_await(
                            lambda msg_seq: self.api.post_group_message(
                                group_openid=group_openid,
                                msg_type=7,
                                content="",
                                media={"file_info": file_info},
                                msg_seq=msg_seq,
                            ),
                        )
                    else:
                        raise
        except QQReplyBudgetExceeded:
            logger.info("qq_bot.audio_budget_exhausted")
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
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False
                )
                if result.returncode != 0:
                    logger.warning("qq_bot.ffmpeg_failed", stderr=result.stderr[:200])
                    return False
                pilk.encode(str(pcm_path), str(silk_path), pcm_rate=16000, tencent=True)
                return True

            ok = await to_thread_heavy(_do_convert)

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
    # 配置初始化收口（独立运行模式；单进程形态下由 web lifespan 覆盖）
    from config_paths import initialize_config
    initialize_config()
    # P0 修复：实时从 env 读取（与 run_qq_bot 保持一致，防止模块级变量未更新）
    _main_app_id = os.getenv("QQBOT_APP_ID", "").strip() or APP_ID
    _main_app_secret = os.getenv("QQBOT_APP_SECRET", "").strip() or APP_SECRET
    if not _main_app_id or _main_app_id == "your_app_id_here":
        # 直启模式的引导信息统一走 loguru：WebUI 单进程下 stdout 是日志管道，
        # 独立终端运行时 loguru 的 stderr 输出同样直达用户
        logger.error(
            "qq_bot.missing_credentials\n"
            "=" * 55 + "\n"
            "  请先配置 QQ Bot AppID 和 AppSecret\n\n"
            "  步骤:\n"
            "  1. 浏览器打开: https://q.qq.com\n"
            "  2. 用手机 QQ 扫码登录\n"
            "  3. 点击「创建机器人」\n"
            "  4. 复制 AppID 和 AppSecret\n"
            "  5. 填入 .env 文件\n" + "=" * 55
        )
        sys.exit(1)

    logger.info(
        f"{get_agent_display_name('xiaoda')}的 QQ Bot 启动中\n"
        + "=" * 50 + "\n"
        "  私聊: 全自动回复\n"
        "  群聊: @机器人 触发\n" + "=" * 50
    )

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
            # __main__ 块为同步上下文（client.run 是同步调用），使用 time.sleep
            time.sleep(delay)

    if retry_count >= MAX_RETRIES:
        logger.error("qq_bot.max_retries_exceeded")
        sys.exit(1)
