"""WebSocket 主通道（§9 协议）：流式状态、工具事件、最终回复、问候/任务/配置广播。"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import platform
import shutil
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from utils.common import safe_int as _safe_int

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    # Windows: 使用 subprocess + 管道模拟终端
    _HAS_PTY = False
else:
    _HAS_PTY = True

from fastapi import APIRouter, WebSocket, WebSocketDisconnect  # noqa: E402
from loguru import logger  # noqa: E402

from agent_core.user_web import WebUser  # noqa: E402
from config import (  # noqa: E402
    STREAM_STATUS_PUSH,
    STREAM_TEXT_PUSH,
    STREAM_TOOL_STATUS,
    STRUCTURED_STREAM_EVENTS,
)
from core.event_bus import event_bus  # noqa: E402
from llm_gateway.stream_protocol import (  # noqa: E402
    StreamEventSequencer,
    StructuredStreamProtocolError,
)

router = APIRouter()

# G2: broadcast 超时阈值（秒）—— 超过即取消任务并清理慢连接，避免慢连接阻塞快连接
BROADCAST_TIMEOUT = 5.0

# G5: 心跳配置 —— 30s 发 ping，10s 内未收 pong 则关闭死连接
HEARTBEAT_INTERVAL = 30  # 秒
HEARTBEAT_TIMEOUT = 10   # 等待 pong 超时

# 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    MEDIA_ROOT = MEDIA_DIR
except ImportError:
    MEDIA_ROOT = Path(__file__).resolve().parent / "media"

# 当前消息处理会话（ContextVar）：随 asyncio.create_task 传播到工具执行链，
# 供命令确认等带身份的事件定位发起连接，避免广播给无关客户端。
_current_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "xiaoda_current_session", default="")


def set_current_session(session_id: str) -> None:
    """在当前异步上下文中记录正在处理的会话 ID。"""
    _current_session.set(session_id or "")


def current_session_id() -> str:
    """读取当前异步上下文中的会话 ID（无会话时为空串）。"""
    return _current_session.get()


class ConnectionManager:
    """连接管理 + 事件广播。"""

    MAX_CONNECTIONS = 32  # 最大并发 WebSocket 连接数（防资源耗尽）

    def __init__(self) -> None:
        """初始化 WebSocket 连接管理器."""
        self._connections: dict[str, WebSocket] = {}
        self._agent_map: dict[str, str] = {}      # conn_id -> 当前受话 agent
        self._session_map: dict[str, str] = {}    # conn_id -> session_id
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        # G5: 心跳状态 —— pong 事件 + 每连接心跳协程
        self._pong_events: dict[str, asyncio.Event] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        # 治本修复：每连接有界发送队列 + 独立写入任务。
        # 根因：send_to/broadcast 直接 await ws.send_json，慢/挂起连接会阻塞调用方
        # （如工具执行路径），导致工具被拖慢、吃掉验证循环墙钟 → 降级。
        # 改为入队即返回，写入由后台任务串行执行，可视化推送不再阻塞主流程。
        self._send_queues: dict[str, asyncio.Queue] = {}
        self._writer_tasks: dict[str, asyncio.Task] = {}
        self._term_start_tasks: set[asyncio.Task] = set()
        # 每连接在途 chat 任务数上限：客户端换 msg_id 即可并发拉起无限 LLM
        # 任务（track_message_task 只按 msg_id 幂等），此处按连接方硬顶
        self.MAX_CHAT_TASKS_PER_CONN = 3
        self._stream_sessions: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._MAX_STREAM_SESSIONS = 256
        self._SEND_QUEUE_MAX = 64  # 有界队列上限；溢出的连接视为过慢并关闭
        # msg_id 幂等：已完成请求的短 TTL 记录（重试帧直接重放而非二次执行）
        self._completed_results: dict[tuple[str, str], float] = {}
        self._MSG_RESULT_TTL_SECONDS = 60.0

    def register(self, ws: WebSocket) -> str:
        """注册一个新连接, 返回生成的连接 ID."""
        if len(self._connections) >= self.MAX_CONNECTIONS:
            raise ValueError(f"连接数已达上限 {self.MAX_CONNECTIONS}，拒绝新连接")
        conn_id = uuid.uuid4().hex[:8]
        self._connections[conn_id] = ws
        self._agent_map[conn_id] = "xiaoda"
        self._session_map[conn_id] = f"web_{uuid.uuid4().hex[:12]}"
        # G5: 初始化 pong 事件 + 启动心跳协程
        self._pong_events[conn_id] = asyncio.Event()
        self._heartbeat_tasks[conn_id] = asyncio.create_task(
            self._heartbeat_loop(conn_id))
        # 治本：初始化发送队列 + 独立写入任务
        self._send_queues[conn_id] = asyncio.Queue(maxsize=self._SEND_QUEUE_MAX)
        self._writer_tasks[conn_id] = asyncio.create_task(
            self._writer_loop(conn_id))
        return conn_id

    async def unregister(self, conn_id: str) -> None:
        """按连接 ID 注销连接及其会话映射.

        P1-4: 改为 async 方法，先 await ws.close() 释放底层 TCP 资源，
        再清理内部状态。close 失败不应阻塞清理（defensive）。幂等：重复调用安全。

        心跳超时路径自取消防护：unregister 可能被 heartbeat 任务自身调用，
        cancel 集合必须排除 asyncio.current_task()——否则取消会在后续 await
        处注入 CancelledError，中断 chat 任务清理导致 LLM/工具泄漏。

        任务回收带超时上限：病态任务（吞掉 CancelledError 后挂起）不得拖死
        unregister——否则心跳路径清理被卡住，连接状态半残。
        """
        ws = self._connections.get(conn_id)
        if ws is not None:
            try:
                await ws.close()
            except (RuntimeError, OSError, ConnectionError) as e:
                # 防御性: 连接可能已被对端关闭，close 抛错不应阻塞清理
                logger.debug("ws.close_failed conn_id={} error={}", conn_id, str(e))
        self._connections.pop(conn_id, None)
        self._agent_map.pop(conn_id, None)
        self._session_map.pop(conn_id, None)
        current = asyncio.current_task()
        # G5: 取消心跳任务 + 清理 pong event（永不取消调用者自身）
        task = self._heartbeat_tasks.pop(conn_id, None)
        if task and task is not current and not task.done():
            task.cancel()
        self._pong_events.pop(conn_id, None)
        # 治本：取消写入任务 + 清理发送队列
        wtask = self._writer_tasks.pop(conn_id, None)
        if wtask and wtask is not current and not wtask.done():
            wtask.cancel()
        self._send_queues.pop(conn_id, None)
        for key in [key for key in self._stream_sessions if key[0] == conn_id]:
            self._stream_sessions.pop(key, None)
        try:
            await asyncio.wait_for(
                self.cancel_connection_tasks(conn_id), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("ws.unregister.reap_timeout conn_id={}", conn_id)

    def track_message_task(self, conn_id: str, msg_id: str, task: asyncio.Task) -> bool:
        """登记在途消息任务（put-if-absent 幂等）。

        返回 False 表示同 (conn_id, msg_id) 已有在途任务——调用方不得重复执行
        （客户端重试重发会造成双份 LLM/工具副作用，且 abort 只能取消后一份）。
        """
        key = (conn_id, msg_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            task.cancel()
            return False
        self._tasks[key] = task

        def _discard(done_task: asyncio.Task) -> None:
            if self._tasks.get(key) is done_task:
                self._tasks.pop(key, None)
                # 完成结果短 TTL 缓存：客户端重试同 msg_id 时直接重放 final，
                # 不再二次执行。
                try:
                    exc = done_task.exception()
                except asyncio.CancelledError:
                    return
                if exc is None:
                    self._completed_results[key] = (
                        time.monotonic() + self._MSG_RESULT_TTL_SECONDS)

        task.add_done_callback(_discard)
        return True

    def get_completed_result_time(self, conn_id: str, msg_id: str) -> float | None:
        """同 msg_id 已完成且仍在 TTL 内时返回完成时间戳（单调钟），否则 None。"""
        deadline = self._completed_results.get((conn_id, msg_id))
        if deadline is None:
            return None
        if time.monotonic() > deadline:
            self._completed_results.pop((conn_id, msg_id), None)
            return None
        return deadline

    def get_message_task(self, conn_id: str, msg_id: str) -> asyncio.Task | None:
        return self._tasks.get((conn_id, msg_id))

    def inflight_chat_count(self, conn_id: str) -> int:
        """该连接当前在途的 chat 任务数（用于每连接并发节流）。"""
        return sum(1 for (owner, _), task in self._tasks.items()
                   if owner == conn_id and not task.done())

    def get_session(self, conn_id: str) -> str:
        """读取连接绑定的会话 ID（封装 _session_map，避免跨模块私有访问）。"""
        return self._session_map.get(conn_id, "")

    def set_session(self, conn_id: str, session_id: str) -> None:
        if session_id:
            self._session_map[conn_id] = session_id

    def get_agent(self, conn_id: str) -> str:
        """读取连接当前受话 agent（封装 _agent_map）。"""
        return self._agent_map.get(conn_id, "xiaoda")

    def set_agent(self, conn_id: str, agent: str) -> None:
        self._agent_map[conn_id] = agent

    def notify_pong(self, conn_id: str) -> None:
        """客户端心跳 pong 应答（封装 _pong_events）。"""
        evt = self._pong_events.get(conn_id)
        if evt:
            evt.set()

    async def cancel_message_task(self, conn_id: str, msg_id: str) -> None:
        task = self.get_message_task(conn_id, msg_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def cancel_connection_tasks(self, conn_id: str) -> None:
        tasks = [
            task for (owner, _), task in self._tasks.items()
            if owner == conn_id
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        for conn_id in list(self._connections):
            await self.unregister(conn_id)
        remaining = list(self._tasks.values())
        for task in remaining:
            if not task.done():
                task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        self._tasks.clear()

    async def _heartbeat_loop(self, conn_id: str) -> None:
        """G5: 每个连接的心跳协程 - 30s ping + 10s pong 超时.

        - 每 HEARTBEAT_INTERVAL 秒发送 ping
        - HEARTBEAT_TIMEOUT 秒内未收到 pong → unregister（死连接）
        - send 失败 → unregister（连接已断）
        - CancelledError 优雅退出（unregister 时取消）
        """
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                ws = self._connections.get(conn_id)
                if ws is None:
                    return
                await ws.send_json({"type": "ping"})
                # pong 处理在 websocket_endpoint 中 set event
                evt = self._pong_events.get(conn_id)
                if evt is None:
                    evt = asyncio.Event()
                    self._pong_events[conn_id] = evt
                evt.clear()
                await asyncio.wait_for(evt.wait(), timeout=HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("ws.heartbeat_timeout conn_id={}", conn_id)
                await self.unregister(conn_id)
                return
            except asyncio.CancelledError:
                return
            except (RuntimeError, OSError, ConnectionError) as e:
                logger.warning("ws.heartbeat_error conn_id={} error={}",
                               conn_id, str(e))
                await self.unregister(conn_id)
                return

    def _stream_frames(self, conn_id: str, event: dict) -> list[dict]:
        """按双帧兼容策略计算应入队的帧列表。

        STRUCTURED_STREAM_EVENTS 开启时，聊天生命周期事件（stream_text/
        tool_status/final/error）同时产出两路帧：
          [0] legacy 帧 —— 供旧客户端（已部署 bundle 只认 stream_text/final 等同名帧）
          [1] stream_event v1 信封 —— 供新前端（seq 单调去重 + 迟到事件防护）
        文本帧两路都只携带绝对 accumulated（由本端按 delta 重建/采信生产者），
        不携带增量 delta：旧客户端（accumulated 绝对覆盖）、新前端
        （delta 为空时回退 accumulated 绝对覆盖）以及同时消费两路的客户端，
        无论到达顺序如何都不会重复拼接文本。返回 [] 表示该事件被结构化协议
        抑制（tool_event 全局广播、终态之后的迟到事件）。
        """
        if not STRUCTURED_STREAM_EVENTS:
            return [event]
        msg_id = str(event.get("msg_id") or "")
        if not msg_id:
            return [event]
        event_type = str(event.get("type") or "")
        # tool_event 是无稳定请求身份的全局广播；结构化模式改用请求绑定的 tool_status。
        if event_type == "tool_event":
            return []
        if event_type not in {
            "stream_text", "tool_status", "final", "error",
        }:
            return [event]
        key = (conn_id, msg_id)
        session = self._stream_sessions.get(key)
        if session is None:
            session = {
                "sequencer": StreamEventSequencer(msg_id),
                "turn": None,
                "accumulated": "",
            }
            self._stream_sessions[key] = session
            while len(self._stream_sessions) > self._MAX_STREAM_SESSIONS:
                self._stream_sessions.popitem(last=False)
        else:
            self._stream_sessions.move_to_end(key)
        terminal = event_type in {"final", "error"}
        payload = {k: v for k, v in event.items() if k not in {"type", "msg_id"}}
        turn = int(payload.pop("turn", 0) or 0)
        mapped_event = {
            "stream_text": "text_delta",
            "tool_status": "tool_status",
            "final": "final",
            "error": "abort" if payload.get("code") == "ABORTED" else "error",
        }[event_type]
        try:
            envelope = session["sequencer"].emit(
                mapped_event, turn=turn, terminal=terminal, **payload,
            )
        except StructuredStreamProtocolError:
            # 终态之后的迟到事件：两路一并抑制，避免旧客户端回放过期内容。
            return []
        if event_type == "stream_text":
            accumulated = self._track_stream_text(session, turn, payload)
            legacy = {
                "type": "stream_text",
                "msg_id": msg_id,
                "accumulated": accumulated,
                "turn": turn,
            }
            envelope.pop("delta", None)
            envelope["accumulated"] = accumulated
        else:
            legacy = event
        return [legacy, envelope]

    @staticmethod
    def _track_stream_text(session: dict[str, Any], turn: int, payload: dict) -> str:
        """维护每条流的绝对文本快照：生产者给的 accumulated 优先，否则按 delta 累积。

        turn 变化视为新一轮文本流，快照从头重建（与逐 turn 流式语义一致）。
        """
        provided = str(payload.get("accumulated") or "")
        delta = str(payload.get("delta") or "")
        if session["turn"] != turn:
            session["turn"] = turn
            session["accumulated"] = provided or delta
        elif provided:
            session["accumulated"] = provided
        else:
            session["accumulated"] += delta
        return session["accumulated"]

    def _enqueue(self, conn_id: str, event: dict) -> bool:
        """非阻塞入队一个事件；连接不存在返回 False。

        队列满（连接过慢）时：丢弃最旧的事件为新事件腾位，而非注销连接。
        这样最新/关键消息（如最终回复）仍能送达，不牺牲功能性；被丢弃的只是
        过期可视化推送。结构化模式下一条逻辑事件可能展开为 legacy+信封双帧
        （见 _stream_frames），每帧独立套用同样的溢出策略；因文本帧均为绝对
        快照，任一帧被挤出队列都不会造成内容错乱。
        """
        frames = self._stream_frames(conn_id, event)
        if not frames:
            return True
        if conn_id not in self._connections:
            return False
        q = self._send_queues.get(conn_id)
        if q is None:
            return False
        for frame in frames:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # 丢最旧，保最新
                except asyncio.QueueEmpty:
                    # 队列已被消费完，无可丢弃事件（正常路径）
                    logger.debug("ws.send_queue_drain_empty conn_id={}", conn_id)
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    logger.warning("ws.send_queue_overflow conn_id={}", conn_id)
                    return False
        return True

    async def send_to(self, conn_id: str, event: dict) -> None:
        """向指定连接发送事件（非阻塞入队；过慢连接关闭）.

        治本修复：不再直接 await ws.send_json，而是入有界队列由写入任务串行发送，
        避免慢/挂起连接阻塞调用方（工具执行路径等关键路径）。
        """
        if not self._enqueue(conn_id, event):
            await self.unregister(conn_id)

    async def broadcast(self, event: dict) -> None:
        """向所有活跃连接广播事件（非阻塞入队；过慢连接关闭）.

        治本修复：入队即返回，写入由各连接后台任务执行，单条慢连接不再拖累
        调用方（工具执行路径）与其它连接。
        """
        for cid in list(self._connections):
            if not self._enqueue(cid, event):
                await self.unregister(cid)

    async def send_to_session(self, session_id: str, event: dict) -> None:
        """仅向指定会话的连接发送事件（无匹配连接时静默忽略）.

        用于命令确认等带身份的事件：只通知发起会话对应连接，
        避免无关客户端收到请求。会话由 set_session 维护在 _session_map。
        """
        if not session_id:
            return
        for cid, sid in list(self._session_map.items()):
            if sid == session_id and cid in self._connections:
                if not self._enqueue(cid, event):
                    await self.unregister(cid)

    async def _writer_loop(self, conn_id: str) -> None:
        """每连接写入任务：串行发送队列中的事件，失败则注销连接."""
        q = self._send_queues.get(conn_id)
        if q is None:
            return
        while True:
            try:
                event = await q.get()
            except asyncio.CancelledError:
                return
            ws = self._connections.get(conn_id)
            if ws is None:
                return
            try:
                await ws.send_json(event)
            except (RuntimeError, OSError) as e:
                logger.debug("ws.send_error conn_id={} error={}", conn_id, str(e))
                # 先把本任务从 writer_tasks 摘除，避免 unregister 自取消冲突
                self._writer_tasks.pop(conn_id, None)
                await self.unregister(conn_id)
                return
            finally:
                q.task_done()

    @property
    def active_count(self) -> int:
        """返回当前活跃连接数."""
        return len(self._connections)


manager = ConnectionManager()


def local_ai_event(resource: str, record: Any) -> dict[str, Any]:
    if resource not in {"device", "download", "instance"}:
        raise ValueError(f"unsupported Local AI resource: {resource}")
    payload = record.to_dict() if hasattr(record, "to_dict") else dict(record)
    return {
        "type": f"local_ai_{resource}_updated",
        resource: payload,
    }



# ── 媒体路径 → URL ───────────────────────────────────────────────


def _publish_file(src: Path | None, kind: str, link: bool = False) -> str | None:
    if not src:
        return None
    src = Path(src)
    if not src.exists():
        return None
    dest_dir = MEDIA_ROOT / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if not dest.exists():
        if link:
            try:
                dest.symlink_to(src.resolve())
            except OSError:
                # Windows: 创建 symlink 需管理员权限，fallback 到复制
                try:
                    shutil.copy2(src, dest)
                except OSError:
                    return None
        else:
            try:
                shutil.copy2(str(src), str(dest))
            except OSError:
                return None
    return f"/media/{kind}/{dest.name}"


# ── 媒体文件定期清理（仅清理动态生成的目录，不碰 stickers/背景图/参考音频）──

_CLEANABLE_KINDS = frozenset({"tts", "image", "video", "upload"})
_MEDIA_MAX_AGE_SECONDS = _safe_int(os.getenv("MEDIA_MAX_AGE_HOURS", "24"), 24) * 3600
_MEDIA_CLEANUP_INTERVAL_SECONDS = _safe_int(os.getenv("MEDIA_CLEANUP_INTERVAL_MINUTES", "60"), 60) * 60
_MEDIA_CLEANUP_TASK: asyncio.Task | None = None


def _cleanup_old_media() -> int:
    """删除超过 MEDIA_MAX_AGE_HOURS 的动态媒体文件（仅 tts/image/video/upload）。"""
    if not MEDIA_ROOT.exists():
        return 0
    now = time.time()
    removed = 0
    for kind in _CLEANABLE_KINDS:
        kind_dir = MEDIA_ROOT / kind
        if not kind_dir.is_dir():
            continue
        for f in kind_dir.iterdir():
            if not f.is_file():
                continue
            try:
                if now - f.stat().st_mtime > _MEDIA_MAX_AGE_SECONDS:
                    f.unlink()
                    removed += 1
            except OSError:
                logger.debug("ws.media_cleanup_unlink_failed: {}", f, exc_info=True)
    if removed:
        logger.info("ws.media_cleanup", removed=removed,
                    max_age_hours=_MEDIA_MAX_AGE_SECONDS // 3600)
    return removed


async def _media_cleanup_loop() -> None:
    """后台循环：定期清理过期动态媒体文件（IO 下放线程池，不占事件循环）。"""
    while True:
        await asyncio.sleep(_MEDIA_CLEANUP_INTERVAL_SECONDS)
        try:
            removed = await asyncio.to_thread(_cleanup_old_media)
        except (OSError, RuntimeError) as e:
            logger.warning("ws.media_cleanup_error", error=str(e))
            continue
        if removed:
            logger.info("ws.media_cleanup", removed=removed,
                        max_age_hours=_MEDIA_MAX_AGE_SECONDS // 3600)


def start_media_cleanup() -> None:
    """启动媒体清理后台任务（幂等，重复调用不会创建多个任务）。"""
    global _MEDIA_CLEANUP_TASK
    if _MEDIA_CLEANUP_TASK is not None:
        return
    _cleanup_old_media()
    _MEDIA_CLEANUP_TASK = asyncio.create_task(_media_cleanup_loop())
    logger.info("ws.media_cleanup_started",
                max_age_hours=_MEDIA_MAX_AGE_SECONDS // 3600,
                interval_minutes=_MEDIA_CLEANUP_INTERVAL_SECONDS // 60)


async def stop_media_cleanup() -> None:
    global _MEDIA_CLEANUP_TASK
    task = _MEDIA_CLEANUP_TASK
    _MEDIA_CLEANUP_TASK = None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def serialize_result(result: Any) -> dict:
    """ProcessResult → 可下发 JSON（媒体路径转 /media/ URL）。"""
    return {
        "reply": result.reply,
        "emotion": result.emotion or "",
        "sticker_url": _publish_file(result.sticker_path, "stickers", link=False),
        "audio_url": _publish_file(result.audio_path, "tts"),
        "image_urls": [u for u in (_publish_file(p, "image")
                                   for p in (result.image_paths or [])) if u],
        "video_url": _publish_file(result.video_path, "video"),
    }


async def _synthesize_speech(core: Any, agent: str, tts_text: str, emotion: str) -> str | None:
    """按 agent 分派 TTS 合成（xiaoda 主体直连，子 agent 未注册时回退主体）。"""
    if agent == "xiaoda":
        return await core.tts.synthesize_xiaoda(tts_text, emotion=emotion)
    sub_agent = core.dispatcher.get_agent(agent)
    if sub_agent:
        return await sub_agent.synthesize(tts_text, emotion=emotion)
    return await core.tts.synthesize_xiaoda(tts_text, emotion=emotion)


async def _async_tts_task(core: Any, agent: str, tts_text: str, emotion: str,
                           conn_id: str, msg_id: str) -> None:
    """Task 6: 后台 TTS 合成任务 -- 合成完成后推送 audio_ready 事件。"""
    try:
        audio_path = await _synthesize_speech(core, agent, tts_text, emotion)
        audio_url = _publish_file(audio_path, "tts") if audio_path else None
        if audio_url:
            await manager.send_to(conn_id, {
                "type": "audio_ready", "msg_id": msg_id, "audio_url": audio_url
            })
        else:
            logger.warning("ws.async_tts_no_audio", conn_id=conn_id, msg_id=msg_id)
    except asyncio.CancelledError:
        raise
    except (OSError, RuntimeError) as e:
        logger.error("ws.async_tts_failed", conn_id=conn_id, msg_id=msg_id, error=str(e))


async def _synthesize_tts_sync(core: Any, agent: str, tts_text: str, emotion: str) -> str | None:
    """同步 TTS 合成（HTTP 端点等无 WebSocket 连接场景的回退）。"""
    try:
        audio_path = await _synthesize_speech(core, agent, tts_text, emotion)
        return _publish_file(audio_path, "tts") if audio_path else None
    except (OSError, RuntimeError) as e:
        logger.error("ws.sync_tts_failed", error=str(e))
        return None


async def _resolve_pending_tts(core: Any, agent: str, result: Any, data: dict,
                                conn_id: str, msg_id: str) -> None:
    """Task 6: 处理 tts_pending 结果 —— WebSocket 走异步，HTTP 走同步回退。"""
    if not getattr(result, "tts_pending", False):
        return
    if conn_id and msg_id:
        # WebSocket：启动后台合成任务，先返回 audio_pending
        data["audio_pending"] = True
        # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致音频丢失
        from core.background_tasks import _spawn
        _spawn(_async_tts_task(
            core, agent, result.tts_text, result.emotion, conn_id, msg_id))
    else:
        # HTTP 端点等无 WS 连接：同步合成回退
        audio_url = await _synthesize_tts_sync(
            core, agent, result.tts_text, result.emotion)
        if audio_url:
            data["audio_url"] = audio_url


async def process_and_serialize(core: Any, text: str, session_id: str,
                                agent: str = "xiaoda",
                                status_callback: Any | None=None, app: Any | None=None,
                                conn_id: str = "", msg_id: str = "",
                                image_data: list[dict] | None = None,
                                system_context: str = "") -> dict:
    """统一处理入口：主体走 process；Web 子代理走 AgentCore 锁内入口。

    斜杠命令（/ 开头）始终走主体 process（内部路由到 SlashCommandHandler）。
    Task 6: 当 TTS_ASYNC_MODE 开启且结果标记 tts_pending 时，启动后台合成任务。
    """
    # 记录当前会话到异步上下文：工具执行链（命令确认等）可据此定位发起连接
    set_current_session(session_id)
    t0 = time.time()
    if agent != "xiaoda" and not text.strip().startswith("/"):
        registry = getattr(app.state, "agent_registry", None) if app else None
        if registry and not registry.is_enabled(agent):
            raise ValueError(f"Agent {agent} 已被禁用")
        if not core.dispatcher.get_agent(agent):
            # 降级模式：子 Agent 未注册时回退到主 Agent，并通知用户
            from loguru import logger as _logger
            _logger.warning("ws.agent_fallback agent={} msg='not registered, falling back to xiaoda'", agent)
            _original_agent = agent
            agent = "xiaoda"
            # 在返回结果中附带降级通知（由 _handle_chat 拼入 data）
            # 此处通过 status_callback 告知前端
            if status_callback:
                try:
                    await status_callback(f"⚠️ {_original_agent} 暂不可用，已切换到小妲回复")
                except (RuntimeError, OSError, ConnectionError):
                    logger.warning("ws.agent_fallback_status_callback_failed", exc_info=True)
        else:
            # Web 直达子代理必须经过 AgentCore 的锁内入口；Web 层不管理用户上下文。
            result = await core.dispatch_web_sub_agent(
                agent,
                text,
                session_id=session_id,
                status_callback=status_callback,
                user_id=os.getenv("MASTER_QQ_OPENID", "webui"),
            )  # Web 直达子代理路径当前不涉及后台委托插话，保持默认 None
            data = serialize_result(result)
            # XP 自动加成：子 agent 路径也需触发 XP（与主路径一致）
            # 根因修复：add_chat_xp 内部 json.dump 同步写文件，原直接调用阻塞事件循环。
            # 用 asyncio.to_thread 隔离到线程池，不阻塞事件循环。
            try:
                from core.xp_system import get_xp_system
                _xp_uid = os.getenv("MASTER_QQ_OPENID", "webui")  # 统一 XP ID
                await asyncio.to_thread(get_xp_system().add_chat_xp, _xp_uid, len(text))
            except (ImportError, RuntimeError, OSError) as _e:
                from loguru import logger as _logger
                _logger.warning("xp.auto_add_failed", error=str(_e))
            data["agent"] = agent
            data["elapsed_ms"] = int((time.time() - t0) * 1000)
            if app is not None and data.get("emotion"):
                app.state.last_emotion = {"primary": data["emotion"], "timestamp": time.time()}
            # Task 6: 异步 TTS —— WebSocket 走异步，HTTP 走同步回退
            await _resolve_pending_tts(core, agent, result, data, conn_id, msg_id)
            return data
    else:
        result = await core.process(
            user_input=text, user_id=os.getenv("MASTER_QQ_OPENID", "webui"), source="web",
            session_id=session_id, status_callback=status_callback,
            image_data=image_data,
            system_context=system_context)  # P0 Task 2.1：传递结构化模式提示
        data = serialize_result(result)
    data["agent"] = agent
    data["elapsed_ms"] = int((time.time() - t0) * 1000)
    if app is not None and data.get("emotion"):
        app.state.last_emotion = {"primary": data["emotion"], "timestamp": time.time()}
    # Task 6: 异步 TTS —— WebSocket 走异步，HTTP 走同步回退
    await _resolve_pending_tts(core, agent, result, data, conn_id, msg_id)
    return data


# ── WebSocket 端点 ───────────────────────────────────────────────


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # 先验证 token 再 accept，防止无 token 连接耗尽资源
    from web.routers.auth import _validate_token

    # 仅从 Sec-WebSocket-Protocol 子协议读取 token（VULN-07：弃用 URL query 传 token）
    subprotocol_token = None
    headers = getattr(ws, "headers", None)
    if headers is not None:
        raw = headers.get("sec-websocket-protocol")
        if raw:
            subprotocol_token = str(raw).split(",")[0].strip()
    subprotocol_token = subprotocol_token or None

    token = subprotocol_token

    if not token or not _validate_token(token):
        # 未授权统一用 4001 关闭（4000-4999 为应用私有段），前端 onclose 据此停止重连
        await ws.close(code=4001, reason="Unauthorized")
        return

    if subprotocol_token:
        await ws.accept(subprotocol=subprotocol_token)
    else:
        await ws.accept()

    try:
        conn_id = manager.register(ws)
    except ValueError:
        await ws.send_json({"type": "error", "code": "MAX_CONNECTIONS",
                            "message": f"连接数已达上限 {manager.MAX_CONNECTIONS}，请稍后重试"})
        await ws.close(code=4029, reason="Too many connections")
        return
    logger.info("ws.connected conn_id={}", conn_id)
    await manager.send_to(conn_id, {
        "type": "connected", "conn_id": conn_id,
        "session_id": manager.get_session(conn_id),
    })

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type", "")
            await _dispatch_message(conn_id, msg, mtype, ws)

    except WebSocketDisconnect:
        logger.info("ws.disconnected conn_id={}", conn_id)
    except (RuntimeError, OSError, asyncio.CancelledError, KeyError, TypeError) as e:
        logger.error("ws.error conn_id={} error={}", conn_id, str(e))
    finally:
        # 清理该连接的所有终端会话
        with _pty_sessions_lock:
            sids = list(_pty_sessions.keys())
        for sid in sids:
            with _pty_sessions_lock:
                sid_session = _pty_sessions.get(sid)
            if sid_session and sid_session.get("conn_id") == conn_id:
                _cleanup_pty(sid)
        await manager.unregister(conn_id)


async def _dispatch_message(conn_id: str, msg: dict, mtype: str, ws: WebSocket) -> None:
    """按消息类型分发处理（ping/pong/set_agent/set_session/chat/terminal_*/abort）。"""
    if mtype == "ping":
        await manager.send_to(conn_id, {"type": "pong"})

    elif mtype == "pong":
        # G5: 客户端响应心跳 pong -- 唤醒心跳协程的 wait_for
        manager.notify_pong(conn_id)

    elif mtype == "set_agent":
        agent = str(msg.get("agent") or "xiaoda")
        manager.set_agent(conn_id, agent)
        await manager.send_to(conn_id, {"type": "agent_changed", "agent": agent})

    elif mtype == "set_session":
        sid = str(msg.get("session_id") or "")
        if sid:
            manager.set_session(conn_id, sid)
            await manager.send_to(conn_id, {"type": "session_changed", "session_id": sid})

    elif mtype == "chat":
        msg_id = str(msg.get("msg_id") or uuid.uuid4().hex[:8])
        # 幂等防线 1：已完成且在 TTL 内的同 msg_id 直接重放，不二次执行
        if msg_id and manager.get_completed_result_time(conn_id, msg_id) is not None:
            await manager.send_to(conn_id, {
                "type": "error", "msg_id": msg_id,
                "code": "DUPLICATE_COMPLETED",
                "message": "该消息已处理完成，请勿重复发送",
            })
            return
        # 每连接并发节流：track_message_task 只按 msg_id 幂等，客户端换
        # msg_id 即可无限并发拉起 LLM 任务，此处按连接硬顶在途数
        if manager.inflight_chat_count(conn_id) >= manager.MAX_CHAT_TASKS_PER_CONN:
            await manager.send_to(conn_id, {
                "type": "error", "msg_id": msg_id,
                "code": "TOO_MANY_INFLIGHT",
                "message": "当前连接并发请求过多，请等待在途消息完成",
            })
            return
        task = asyncio.create_task(_handle_chat(conn_id, msg, msg_id, ws))
        # 幂等防线 2：同 key 在途时拒绝新帧（put-if-absent），防止双跑副作用
        if not manager.track_message_task(conn_id, msg_id, task):
            await manager.send_to(conn_id, {
                "type": "error", "msg_id": msg_id,
                "code": "DUPLICATE_IN_FLIGHT",
                "message": "相同消息正在处理中，已忽略重复请求",
            })

    elif mtype == "terminal_start":
        term_sid = str(msg.get("term_sid") or uuid.uuid4().hex[:8])
        _t = asyncio.create_task(_handle_terminal_start(conn_id, msg, term_sid))
        manager._term_start_tasks.add(_t)

        def _on_term_start_done(t):
            manager._term_start_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning("ws.terminal_start_task_error: {}", exc)

        _t.add_done_callback(_on_term_start_done)

    elif mtype == "terminal_input":
        _handle_terminal_input(conn_id, msg)

    elif mtype == "terminal_resize":
        _handle_terminal_resize(conn_id, msg)

    elif mtype == "terminal_kill":
        _handle_terminal_kill(conn_id, msg)

    elif mtype == "abort":
        await manager.cancel_message_task(
            conn_id, str(msg.get("msg_id") or "")
        )


def _verify_response(data: dict, msg_id: str, agent: str) -> None:
    """S2: VERIFY 阶段 — 检查响应质量，仅记录警告不修改数据."""
    reply = (data.get("reply") or data.get("text") or "").strip()
    # 1. 空响应或过短响应
    if not reply:
        logger.warning("ws.chat.verify", issue="empty_response", agent=agent, msg_id=msg_id)
    elif len(reply) < 2:
        logger.warning("ws.chat.verify", issue="short_response", agent=agent,
                       msg_id=msg_id, length=len(reply))
    # 2. 工具错误循环检测（关键词 + 频次）
    error_keywords = ("错误:", "Error:", "失败", "failed", "异常", "exception")
    error_lines = [ln for ln in reply.splitlines()
                   if any(kw in ln for kw in error_keywords)]
    if error_lines:
        from collections import Counter
        # 完全相同行重复 >=3 次 → 严重循环
        common = Counter(error_lines).most_common(1)
        if common and common[0][1] >= 3:
            logger.warning("ws.chat.verify", issue="tool_error_loop", agent=agent,
                           msg_id=msg_id, count=common[0][1])
        # 错误行总数 >=5 → 密集错误
        elif len(error_lines) >= 5:
            logger.warning("ws.chat.verify", issue="dense_errors", agent=agent,
                           msg_id=msg_id, count=len(error_lines))
    # 3. 降级响应检测
    if "DEGRADED" in reply or "降级" in reply:
        logger.warning("ws.chat.verify", issue="degraded_reply", agent=agent, msg_id=msg_id)


def build_chat_request_context(msg: dict) -> dict:
    text = str(msg.get("text") or "").strip()
    attachments: list[dict[str, str]] = []
    upload_root = (MEDIA_ROOT / "upload").resolve()
    image_url = str(msg.get("image_url") or "").strip()
    if image_url.startswith("/media/upload/"):
        image_path = (upload_root / Path(image_url).name).resolve()
        expected_url = f"/media/upload/{image_path.name}"
        if image_url == expected_url and image_path.parent == upload_root and image_path.is_file():
            image_name = Path(str(msg.get("image_name") or image_path.name)).name[:255]
            attachments.append({
                "kind": "image", "url": image_url, "name": image_name,
            })
    doc_path = str(msg.get("doc_path") or "").strip()
    if not doc_path:
        import re as _re
        doc_marker = _re.search(r'\n?\[Doc:\s*([^\]]+)\]\s*', text)
        if doc_marker:
            doc_path = doc_marker.group(1).strip()
            text = text.replace(doc_marker.group(0), "").strip()
    if doc_path:
        candidate = Path(doc_path).resolve()
        if candidate.parent == upload_root and candidate.is_file():
            doc_name = Path(str(msg.get("doc_name") or candidate.name)).name[:255]
            attachment = {
                "kind": "document",
                "url": f"/media/upload/{candidate.name}",
                "name": doc_name,
                "path": str(candidate),
            }
            ext = candidate.suffix.lower()
            if ext:
                attachment["ext"] = ext
            attachments.append(attachment)
    return {
        "text": text,
        "search": bool(msg.get("search_mode")),
        "think": bool(msg.get("think_mode")),
        "attachments": attachments,
    }


async def _handle_chat(conn_id: str, msg: dict, msg_id: str, ws: WebSocket) -> None:
    request_context = build_chat_request_context(msg)
    text = request_context["text"]
    image_attachment = next(
        (item for item in request_context["attachments"] if item["kind"] == "image"),
        None,
    )
    document_attachment = next(
        (item for item in request_context["attachments"] if item["kind"] == "document"),
        None,
    )
    image_url_field = image_attachment["url"] if image_attachment else ""
    doc_path_field = document_attachment["path"] if document_attachment else ""
    if not text and not image_url_field and not doc_path_field:
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "EMPTY_REQUEST", "message": "消息或附件不能为空",
        })
        return
    if not text:
        text = "📷 图片" if image_url_field else f"📄 {Path(doc_path_field).name}"
    agent = str(msg.get("agent") or manager.get_agent(conn_id))
    session_id = str(msg.get("session_id") or
                     manager.get_session(conn_id) or f"web_{uuid.uuid4().hex[:12]}")
    manager.set_session(conn_id, session_id)
    app = ws.scope.get("app")
    core = app.state.core

    from web._msg_context import current_msg_id
    token = current_msg_id.set(msg_id)
    from core.background_tasks import (
        reset_current_request_context,
        set_current_request_context,
    )
    request_context_token = set_current_request_context(request_context)

    # P0 修复（Task 2.1）：提取结构化字段（按钮状态走独立字段，不再从 text 解析 marker）
    search_mode = bool(msg.get("search_mode"))
    think_mode = bool(msg.get("think_mode"))

    image_data, text = _build_image_data(image_url_field, text)

    _structured_system_context = _build_structured_hints(search_mode, think_mode, doc_path_field)

    on_status = _make_status_callback(conn_id, msg_id)

    try:
        # ── S2: PLAN 阶段 ──
        try:
            from prompt_builder import _classify_scene
            scene = _classify_scene(text)
            logger.info("ws.chat.phase", phase="plan", scene=scene, agent=agent, msg_id=msg_id)
        except (ImportError, AttributeError, ValueError):
            logger.debug("ws.chat.classify_scene_skip", exc_info=True)

        if STREAM_STATUS_PUSH:
            await manager.send_to(conn_id, {"type": "status", "msg_id": msg_id, "stage": "thinking"})
        # ── S2: EXECUTE 阶段 ──
        logger.info("ws.chat.phase", phase="execute", agent=agent, msg_id=msg_id)
        # 绑定 WebUser 到 EventBus
        async def _ws_send(event: dict) -> None:
            await manager.send_to(conn_id, event)
        web_user = WebUser(send_fn=_ws_send)
        _eb_token = event_bus.bind_user(web_user)
        try:
            data = await process_and_serialize(
                core, text, session_id=session_id, agent=agent,
                status_callback=on_status, app=app,
                conn_id=conn_id, msg_id=msg_id,
                image_data=image_data,
                system_context=_structured_system_context)  # P0 Task 2.1：结构化模式提示
        finally:
            event_bus.unbind_user(_eb_token)
        # ── S2: VERIFY 阶段 ──
        _verify_response(data, msg_id, agent)
        data.update({"type": "final", "msg_id": msg_id})
        await manager.send_to(conn_id, data)
    except asyncio.CancelledError:
        # 取消须向上传播（吞掉会破坏 asyncio 取消语义，任务无法真正终止）。
        # ABORTED 通知经 finally 前的队列发送：send_to 是非阻塞入队，
        # 不会因连接失效而挂起；随后 raise 恢复取消链。
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "ABORTED", "message": "已中断生成"})
        raise
    except (RuntimeError, OSError, ValueError):
        # 异常原文不回传客户端（可能带内部路径/模型细节），完整信息进日志
        logger.exception("ws.chat.failed conn_id={} msg_id={}", conn_id, msg_id)
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "CHAT_ERROR", "message": "生成回复失败，请稍后重试或查看服务端日志"})
    finally:
        reset_current_request_context(request_context_token)
        current_msg_id.reset(token)


def _build_image_data(image_url_field: str, text: str) -> tuple[list | None, str]:
    """构建 image_data（优先结构化字段，兜底解析 [Image:] marker），返回 (image_data, 清理后的 text)。"""
    image_data = None
    image_urls: list[str] = []
    if image_url_field:
        image_urls.append(image_url_field)
    else:
        # 向后兼容：旧客户端在 text 中嵌入 [Image: URL] marker
        import re as _re
        _marker_urls = _re.findall(r'\[Image:\s*([^\]]+)\]', text)
        if _marker_urls:
            image_urls.extend(_marker_urls)
            # 从 text 中剥离 [Image:] marker（保持 text 纯净）
            text = _re.sub(r'\n?\[Image:\s*[^\]]+\]\s*', '', text).strip()
            if not text:
                text = "📷 图片"  # 仅发图片时给一个占位符

    if image_urls:
        from pathlib import Path as _Path

        from utils.text_utils import encode_image_to_base64
        image_data = []
        for url in image_urls:
            try:
                # URL 格式: /media/upload/xxx.png → 映射到本地文件
                local_path = MEDIA_ROOT / "upload" / _Path(url).name
                if local_path.exists():
                    mime, img_b64 = encode_image_to_base64(str(local_path))
                    if not img_b64 or not img_b64.strip() or len(img_b64) < 100:
                        logger.warning("ws_hub_image_skip: url={}, reason=invalid_base64 len={}", url, len(img_b64) if img_b64 else 0)
                        continue
                    image_data.append({"mimeType": mime, "data": img_b64})
                    logger.info("ws.image_loaded url={} size={}KB", url, len(img_b64) // 1024)
                else:
                    logger.warning("ws.image_not_found url={} path={}", url, local_path)
            except (OSError, ValueError, AttributeError) as e:
                logger.warning("ws.image_load_failed url={} error={}", url, str(e))

    return image_data, text


def _build_structured_hints(search_mode: bool, think_mode: bool, doc_path_field: str) -> str:
    """构建模式上下文（search/think/doc 走 system_context，不污染 text）。

    新客户端的结构化字段在这里转换为 system_context 传入；
    旧客户端的 [Search:]/[Think:]/[Doc:] marker 仍由 message_processor 解析。
    """
    _structured_mode_hints: list[str] = []
    if search_mode:
        _structured_mode_hints.append("本次回复请优先使用 web_search 工具搜索最新信息后回答。")
    if think_mode:
        _structured_mode_hints.append("本次回复请进行更深入的思考，可以分步骤推理。")
    if doc_path_field:
        _structured_mode_hints.append(
            f"用户上传了文档：{doc_path_field}。请使用 document_reader 工具读取该文档内容后回答用户的问题。"
        )
    return "\n".join(_structured_mode_hints) if _structured_mode_hints else ""


def _make_status_callback(conn_id: str, msg_id: str):
    """构造流式状态推送回调（受 STREAM_STATUS_PUSH / STREAM_TEXT_PUSH / STREAM_TOOL_STATUS 控制）。"""
    async def on_status(message: Any) -> None:
        # P0: 流式文本推送 —— 独立于 STREAM_STATUS_PUSH，由 STREAM_TEXT_PUSH 控制
        if STREAM_TEXT_PUSH and isinstance(message, dict) and message.get("type") == "stream_text":
            await manager.send_to(conn_id, {
                "type": "stream_text",
                "msg_id": msg_id,
                "delta": message.get("delta", ""),
                "accumulated": message.get("accumulated", ""),
            })
            return
        # P0: 工具调用中间状态推送 —— 由 STREAM_TOOL_STATUS 控制
        if STREAM_TOOL_STATUS and isinstance(message, dict) and message.get("type") == "tool_status":
            await manager.send_to(conn_id, {
                "type": "tool_status",
                "msg_id": msg_id,
                "tool": message.get("tool", ""),
                "stage": message.get("stage", ""),
                "label": message.get("label", ""),
                "detail": message.get("detail", ""),
                "tool_call_id": message.get("tool_call_id", ""),
                "turn": message.get("turn", 0),
                "index": message.get("index", 0),
            })
            return
        if STREAM_STATUS_PUSH:
            await manager.send_to(conn_id, {
                "type": "status", "msg_id": msg_id,
                "stage": "thinking", "text": str(message)[:200],
            })
    return on_status

# ── 虚空终端子模块（web/ws_terminal.py，2026-08-25 拆分）──────────
# 状态与会话管理函数全部内聚于子模块;此处 re-export 保持
# `from web.ws_hub import X` 与 hub._X 引用面不破
# （tests/test_terminal_output_coalescing.py 等 64 处引用）。
from web.ws_terminal import (  # noqa: F401, E402 —— 文件尾 re-export(拆分兼容层)
    _TERM_FLUSH_INTERVAL_S,
    _TERM_FLUSH_MAX_CHARS,
    _cleanup_pty,
    _handle_terminal_input,
    _handle_terminal_kill,
    _handle_terminal_resize,
    _handle_terminal_start,
    _notify_terminal_exit,
    _pty_sessions,
    _pty_sessions_lock,
    _queue_term_output,
    _reap_unix_child,
    _setup_pty_reader,
    _setup_win_pipe_reader,
    _setup_win_pty_reader,
    _term_out_buf,
    _try_import_winpty,
)
