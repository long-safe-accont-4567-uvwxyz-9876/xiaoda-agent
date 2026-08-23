"""WebSocket 主通道（§9 协议）：流式状态、工具事件、最终回复、问候/任务/配置广播。"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import platform
import shutil
import signal
import struct
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from utils.common import safe_int as _safe_int

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    # Windows: 使用 subprocess + 管道模拟终端
    import subprocess as _subprocess
    _HAS_PTY = False
else:
    import fcntl
    import pty
    import termios
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
        self._stream_sessions: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._MAX_STREAM_SESSIONS = 256
        self._SEND_QUEUE_MAX = 64  # 有界队列上限；溢出的连接视为过慢并关闭

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
        # G5: 取消心跳任务 + 清理 pong event
        task = self._heartbeat_tasks.pop(conn_id, None)
        if task and not task.done():
            task.cancel()
        self._pong_events.pop(conn_id, None)
        # 治本：取消写入任务 + 清理发送队列
        wtask = self._writer_tasks.pop(conn_id, None)
        if wtask and not wtask.done():
            wtask.cancel()
        self._send_queues.pop(conn_id, None)
        for key in [key for key in self._stream_sessions if key[0] == conn_id]:
            self._stream_sessions.pop(key, None)
        await self.cancel_connection_tasks(conn_id)

    def track_message_task(self, conn_id: str, msg_id: str, task: asyncio.Task) -> None:
        key = (conn_id, msg_id)
        self._tasks[key] = task

        def _discard(done_task: asyncio.Task) -> None:
            if self._tasks.get(key) is done_task:
                self._tasks.pop(key, None)

        task.add_done_callback(_discard)

    def get_message_task(self, conn_id: str, msg_id: str) -> asyncio.Task | None:
        return self._tasks.get((conn_id, msg_id))

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

# PTY 终端会话: term_sid -> {pid, fd, conn_id, shell, alive}
_pty_sessions: dict[str, dict] = {}
_pty_sessions_lock = threading.Lock()

# 终端输出合帧缓冲: term_sid -> {"buf": str, "timer": asyncio.TimerHandle|None}
# PTY 大输出会被内核拆成大量小块(实测 288KB/2188 次 read)，逐条发 JSON 帧
# 会把前端 xterm 渲染冲垮——按 ~16ms/帧合并后发送。
_term_out_buf: dict[str, dict] = {}
_TERM_FLUSH_INTERVAL_S = 0.016
_TERM_FLUSH_MAX_CHARS = 65536


def _try_import_winpty():
    """ConPTY 可用性探测（仅 win32 有轮子）：返回 PtyProcess 类或 None。

    Windows 会话优先 ConPTY（真终端语义：resize/TUI 全支持），
    未安装 pywinpty 时回退 subprocess 管道（无 TTY，兼容旧行为）。"""
    if os.name != "nt":
        return None
    try:
        from winpty import PtyProcess  # noqa: PLC0415 —— 平台可选依赖懒加载
        return PtyProcess
    except (ImportError, OSError):
        logger.debug("ws.winpty_unavailable: 回退管道模式")
        return None


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
    """后台循环：定期清理过期动态媒体文件。"""
    while True:
        await asyncio.sleep(_MEDIA_CLEANUP_INTERVAL_SECONDS)
        try:
            _cleanup_old_media()
        except (OSError, RuntimeError) as e:
            logger.warning("ws.media_cleanup_error", error=str(e))


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


async def _async_tts_task(core: Any, agent: str, tts_text: str, emotion: str,
                           conn_id: str, msg_id: str) -> None:
    """Task 6: 后台 TTS 合成任务 —— 合成完成后推送 audio_ready 事件。"""
    try:
        if agent == "xiaoda":
            audio_path = await core.tts.synthesize_xiaoda(tts_text, emotion=emotion)
        else:
            sub_agent = core.dispatcher.get_agent(agent)
            if sub_agent:
                audio_path = await sub_agent.synthesize(tts_text, emotion=emotion)
            else:
                audio_path = await core.tts.synthesize_xiaoda(tts_text, emotion=emotion)

        audio_url = _publish_file(audio_path, "tts") if audio_path else None
        if audio_url:
            await manager.send_to(conn_id, {
                "type": "audio_ready", "msg_id": msg_id, "audio_url": audio_url
            })
        else:
            logger.warning("ws.async_tts_no_audio", conn_id=conn_id, msg_id=msg_id)
    except (OSError, RuntimeError, asyncio.CancelledError) as e:
        logger.error("ws.async_tts_failed", conn_id=conn_id, msg_id=msg_id, error=str(e))


async def _synthesize_tts_sync(core: Any, agent: str, tts_text: str, emotion: str) -> str | None:
    """同步 TTS 合成（HTTP 端点等无 WebSocket 连接场景的回退）。"""
    try:
        if agent == "xiaoda":
            audio_path = await core.tts.synthesize_xiaoda(tts_text, emotion=emotion)
        else:
            sub_agent = core.dispatcher.get_agent(agent)
            if sub_agent:
                audio_path = await sub_agent.synthesize(tts_text, emotion=emotion)
            else:
                audio_path = await core.tts.synthesize_xiaoda(tts_text, emotion=emotion)
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
    """统一处理入口：主体走 AgentCore.process；子代理直达 dispatcher（R5）。

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
            # 走与 QQ 通道相同的完整子代理流程：表情包/情绪/TTS/落库都不缺
            from loguru import logger as _logger

            from agent_core import RequestContext
            from utils.trace_context import new_trace_id
            # 身份解析：与 core.process() 主路径一致，确保 is_master/user_openid 语义正确
            _identity = core._resolve_identity("webui", user_openid="", source="web")
            ctx = RequestContext(session_id=session_id, user_id=os.getenv("MASTER_QQ_OPENID", "webui"),
                                 user_input=text, status_callback=status_callback,
                                 is_master=_identity.is_owner)
            ctx.identity = _identity
            _tid = new_trace_id()
            trace = _logger.bind(trace_id=_tid)
            result = await core._dispatch_single_sub_agent(
                agent, text, user_id=os.getenv("MASTER_QQ_OPENID", "webui"), source="web",
                session_id=session_id, trace=trace, ctx=ctx)
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
        "session_id": manager._session_map[conn_id],
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
        # G5: 客户端响应心跳 pong —— 唤醒心跳协程的 wait_for
        evt = manager._pong_events.get(conn_id)
        if evt:
            evt.set()

    elif mtype == "set_agent":
        agent = str(msg.get("agent") or "xiaoda")
        manager._agent_map[conn_id] = agent
        await manager.send_to(conn_id, {"type": "agent_changed", "agent": agent})

    elif mtype == "set_session":
        sid = str(msg.get("session_id") or "")
        if sid:
            manager._session_map[conn_id] = sid
            await manager.send_to(conn_id, {"type": "session_changed", "session_id": sid})

    elif mtype == "chat":
        msg_id = str(msg.get("msg_id") or uuid.uuid4().hex[:8])
        task = asyncio.create_task(_handle_chat(conn_id, msg, msg_id, ws))
        manager.track_message_task(conn_id, msg_id, task)

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
    agent = str(msg.get("agent") or manager._agent_map.get(conn_id, "xiaoda"))
    session_id = str(msg.get("session_id") or
                     manager._session_map.get(conn_id) or f"web_{uuid.uuid4().hex[:12]}")
    manager._session_map[conn_id] = session_id
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
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "ABORTED", "message": "已中断生成"})
    except (RuntimeError, OSError, asyncio.CancelledError, ValueError) as e:
        logger.error("ws.chat.failed conn_id={} error={}", conn_id, str(e))
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "CHAT_ERROR", "message": str(e)[:300]})
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

async def _handle_terminal_start(conn_id: str, msg: dict, term_sid: str) -> None:
    """启动一个终端会话：Linux 用 PTY，Windows 用 subprocess 管道。

    msg 字段：
      shell    — Shell 类型 (bash/zsh/python/node/cmd/powershell/wsl)，默认 bash
      cols     — 终端列数
      rows     — 终端行数
    """
    # P0(技术债审查)：sid 已存在时必须拒绝而非覆盖——否则第二个连接可抢占
    # 他人会话（旧 fd/进程成孤儿、旧 reader 向劫持者连接串输出）。
    with _pty_sessions_lock:
        if term_sid in _pty_sessions:
            logger.warning("ws.terminal.start.duplicate conn_id={} term_sid={}",
                           conn_id, term_sid)
            await manager.send_to(conn_id, {
                "type": "terminal_error", "term_sid": term_sid,
                "error": "term_sid already exists"})
            return
    shell_type = (msg.get("shell") or "bash").strip().lower()
    try:
        cols = int(msg.get("cols") or 80)
        rows = int(msg.get("rows") or 24)
        if not (2 <= cols <= 500 and 2 <= rows <= 200):
            raise ValueError
    except (TypeError, ValueError):
        cols, rows = 80, 24

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    if _HAS_PTY:
        # ── Linux / macOS: PTY 方式 ──
        shell_map = {
            "bash": "bash", "zsh": "zsh",
            "python": "python3", "node": "node",
        }
        shell_cmd = shell_map.get(shell_type, "bash")
        env["SHELL"] = shell_cmd
        loop = asyncio.get_running_loop()

        try:
            child_pid, master_fd = pty.fork()
            if child_pid == 0:
                # ── 子进程 ──
                os.chdir(str(Path.home()))
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                os.execvpe(shell_cmd, [shell_cmd], env)
            else:
                with _pty_sessions_lock:
                    _pty_sessions[term_sid] = {
                        "pid": child_pid, "fd": master_fd, "conn_id": conn_id,
                        "shell": shell_type, "alive": True, "loop": loop,
                        "is_windows": False,
                    }
                logger.info("ws.terminal.start term_sid={} shell={} pid={}", term_sid, shell_type, child_pid)
                await manager.send_to(conn_id, {
                    "type": "terminal_started", "term_sid": term_sid, "shell": shell_type})
                _setup_pty_reader(term_sid)

        except (OSError, RuntimeError, ValueError) as e:
            logger.error("ws.terminal.start.failed term_sid={} error={}", term_sid, str(e))
            await manager.send_to(conn_id, {
                "type": "terminal_error", "term_sid": term_sid,
                "error": str(e)[:200]})
    else:
        # ── Windows: ConPTY 优先（真终端语义），缺 pywinpty 回退管道 ──
        shell_map_win = {
            "cmd": "cmd.exe",
            "powershell": "powershell.exe",
            "pwsh": "pwsh.exe",
            "python": "python.exe",
            "node": "node.exe",
            "wsl": "wsl.exe",
            "bash": "bash.exe",
        }
        exe = shell_map_win.get(shell_type, "cmd.exe")
        loop = asyncio.get_running_loop()
        PtyProcess = _try_import_winpty()

        if PtyProcess is not None:
            # ConPTY：真 PTY——resize/TUI(opencode 等)全支持
            try:
                pty_proc = PtyProcess.spawn(
                    exe, cwd=str(Path.home()), dimensions=(rows, cols),
                    env=list(f"{k}={v}" for k, v in env.items()))
                with _pty_sessions_lock:
                    _pty_sessions[term_sid] = {
                        "pid": pty_proc.pid, "winpty": pty_proc,
                        "conn_id": conn_id, "shell": shell_type,
                        "alive": True, "loop": loop,
                        "is_windows": True, "conpty": True,
                    }
                logger.info("ws.terminal.start term_sid={} shell={} pid={} mode=conpty",
                            term_sid, shell_type, pty_proc.pid)
                await manager.send_to(conn_id, {
                    "type": "terminal_started", "term_sid": term_sid,
                    "shell": shell_type, "mode": "conpty"})
                _setup_win_pty_reader(term_sid)
                return
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("ws.conpty_spawn_failed term_sid={} error={} → 回退管道",
                               term_sid, str(e)[:150])

        # 管道回退：无 TTY 语义（resize no-op、全屏 TUI 不可用）
        try:
            proc = _subprocess.Popen(
                [exe] if not exe.endswith("powershell.exe") and not exe.endswith("pwsh.exe")
                else [exe, "-NoLogo"],
                stdin=_subprocess.PIPE,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.STDOUT,
                bufsize=0,
                env=env,
                cwd=str(Path.home()),
                creationflags=_subprocess.CREATE_NEW_PROCESS_GROUP
                    if hasattr(_subprocess, "CREATE_NEW_PROCESS_GROUP") else 0,
            )
            with _pty_sessions_lock:
                _pty_sessions[term_sid] = {
                    "pid": proc.pid, "proc": proc, "conn_id": conn_id,
                    "shell": shell_type, "alive": True, "loop": loop,
                    "is_windows": True, "conpty": False,
                }
            logger.info("ws.terminal.start term_sid={} shell={} pid={} mode=pipe",
                        term_sid, shell_type, proc.pid)
            await manager.send_to(conn_id, {
                "type": "terminal_started", "term_sid": term_sid, "shell": shell_type})
            _setup_win_pipe_reader(term_sid)

        except (OSError, RuntimeError, ValueError) as e:
            logger.error("ws.terminal.start.failed term_sid={} error={}", term_sid, str(e))
            await manager.send_to(conn_id, {
                "type": "terminal_error", "term_sid": term_sid,
                "error": str(e)[:200]})


def _setup_pty_reader(term_sid: str) -> None:
    """用 loop.add_reader() 注册 PTY fd 的可读回调。"""
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
    if not session:
        return
    fd = session["fd"]
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    def _on_pty_readable() -> None:
        """当 PTY master fd 有数据可读时被调用。"""
        try:
            data = os.read(fd, 8192)
        except OSError:
            _cleanup_pty(term_sid)
            return

        if not data:
            _cleanup_pty(term_sid)
            return

        text = data.decode("utf-8", errors="replace")

        # 输出推送到前端：合帧节流（~16ms 一帧合并多次 read，防前端渲染冲垮）
        _queue_term_output(term_sid, conn_id, text)

        # 送入标记符检测器（内部按行缓冲）
        try:
            from web.pty_executor import feed_output
            feed_output(text)
        except (ImportError, OSError, RuntimeError):
            logger.debug("ws.feed_output_error", exc_info=True)

    loop.add_reader(fd, _on_pty_readable)


def _queue_term_output(term_sid: str, conn_id: str, text: str) -> None:
    """终端输出合帧：缓冲当前块并调度 ~16ms 后的冲刷（在事件循环线程执行）。

    超过单帧上限立即冲刷，避免单条巨帧占内存。"""
    loop: asyncio.AbstractEventLoop | None = None
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if session is not None:
            loop = session.get("loop")
    if loop is None:
        return

    def _flush(term_sid: str = term_sid) -> None:
        entry = _term_out_buf.pop(term_sid, None)
        if not entry or not entry["buf"]:
            return
        sid_ = entry["conn_id"]
        asyncio.ensure_future(manager.send_to(sid_, {
            "type": "terminal_output", "term_sid": term_sid,
            "data": entry["buf"]}))

    with _pty_sessions_lock:
        entry = _term_out_buf.get(term_sid)
        if entry is None:
            entry = {"buf": "", "conn_id": conn_id, "timer": None}
            _term_out_buf[term_sid] = entry
    entry["buf"] += text
    if len(entry["buf"]) >= _TERM_FLUSH_MAX_CHARS:
        # 已满：取消定时器立即发（保持顺序——仍在循环线程串行执行）
        if entry["timer"] is not None:
            entry["timer"].cancel()
        _flush()
        return
    if entry["timer"] is None and loop is not None:
        entry["timer"] = loop.call_later(_TERM_FLUSH_INTERVAL_S, _flush)


def _setup_win_pty_reader(term_sid: str) -> None:
    """Windows ConPTY：后台线程读 PtyProcess 输出，推回事件循环（合帧）。"""
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
    if not session:
        return
    pty_proc = session["winpty"]
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    def _reader_thread() -> None:
        try:
            while pty_proc.isalive():
                # pywinpty read 在无数据时短暂阻塞，返回空串继续轮询
                data = pty_proc.read(8192)
                if not data:
                    if not pty_proc.isalive():
                        break
                    time.sleep(0.01)
                    continue
                loop.call_soon_threadsafe(
                    _queue_term_output, term_sid, conn_id, data)
        except (OSError, RuntimeError, EOFError):
            logger.debug("ws.conpty_reader_error term_sid={}", term_sid,
                         exc_info=True)
        finally:
            loop.call_soon_threadsafe(_cleanup_pty, term_sid)

    import threading
    t = threading.Thread(target=_reader_thread, daemon=True)
    t.start()


def _setup_win_pipe_reader(term_sid: str) -> None:
    """Windows: 在后台线程中读取 subprocess stdout 管道。"""
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
    if not session:
        return
    proc = session["proc"]
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    def _reader_thread() -> None:
        """后台线程：阻塞读取 stdout，推送到 event loop。"""
        try:
            while True:
                data = proc.stdout.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                loop.call_soon_threadsafe(_queue_term_output, term_sid, conn_id, text)

                # 送入标记符检测器（内部按行缓冲）
                try:
                    from web.pty_executor import feed_output
                    feed_output(text)
                except (ImportError, OSError, RuntimeError):
                    logger.debug("ws.feed_output_error_win", exc_info=True)
        except (OSError, RuntimeError):
            logger.debug("ws.win_pipe_reader_error term_sid={}", term_sid, exc_info=True)
        finally:
            loop.call_soon_threadsafe(_cleanup_pty, term_sid)

    import threading
    t = threading.Thread(target=_reader_thread, daemon=True)
    t.start()


def _reap_unix_child(pid: int) -> int:
    """线程池内执行：SIGKILL 补刀 + 有界轮询收割，返回真实退出码。

    reader 看到 EOF/EIO 时子进程可能尚未真正退出；旧实现
    waitpid(WNOHANG) 拿到 (0,0) 会被误判为 rc=0 且不再收割 → defunct 堆积。
    轮询最坏 ~300ms，必须离开事件循环线程执行（否则单进程
    WebUI+QQ+WS 共享的 loop 整体停摆）。"""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # 已退出（正常 EOF 路径常见）或无权限
    for _ in range(30):
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return -1  # 已无此子进程（被别处收割）
        if wpid == pid:
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        time.sleep(0.01)
    return -1


def _notify_terminal_exit(loop: asyncio.AbstractEventLoop, conn_id: str,
                          term_sid: str, rc: int) -> None:
    try:
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                manager.send_to(conn_id, {
                    "type": "terminal_exit", "term_sid": term_sid, "returncode": rc
                }), loop=loop))
    except RuntimeError:
        logger.debug("ws.terminal_exit_send_failed term_sid={}", term_sid)


def _cleanup_pty(term_sid: str) -> None:
    """清理终端会话（在 reader 回调中调用，不能 await）。

    注意：本函数只做非阻塞操作；Unix 收割下放线程池，
    terminal_exit 通知由收割完成回调发送。"""
    # 先冲刷残留输出再清缓冲，保证退出前的最后几行不丢
    entry = _term_out_buf.pop(term_sid, None)
    if entry and entry["buf"]:
        if entry.get("timer") is not None:
            entry["timer"].cancel()
        asyncio.ensure_future(manager.send_to(entry["conn_id"], {
            "type": "terminal_output", "term_sid": term_sid,
            "data": entry["buf"]}))
    with _pty_sessions_lock:
        session = _pty_sessions.pop(term_sid, None)
    if not session:
        return
    session["alive"] = False
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]

    if session.get("is_windows", False):
        # ── Windows: ConPTY 优先，其次 subprocess 管道 ──
        wp = session.get("winpty") if session.get("conpty") else None
        if wp is not None:
            rc = -1
            try:
                wp.terminate(force=True)
            except (OSError, RuntimeError):
                logger.debug("ws.conpty_terminate_error", exc_info=True)
            # ConPTY 无 wait 返回码语义，统一 -1（前端只显示退出提示）
        else:
            proc = session.get("proc")
            rc = -1
            if proc:
                try:
                    proc.terminate()
                    rc = proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    logger.debug("ws.process_terminate_error", exc_info=True)
                    try:
                        proc.kill()
                    except (OSError, PermissionError):
                        logger.debug("ws.process_kill_error", exc_info=True)
                    rc = -1
        _notify_terminal_exit(loop, conn_id, term_sid, rc)
        logger.info("ws.terminal.exit term_sid={} rc={}", term_sid, rc)
    else:
        # ── Unix: 关闭 PTY fd（非阻塞）；收割下放线程池 ──
        fd = session["fd"]
        try:
            loop.remove_reader(fd)
        except (OSError, ValueError):
            logger.debug("ws.remove_reader_error", exc_info=True)
        try:
            os.close(fd)
        except OSError:
            logger.debug("ws.close_fd_error", exc_info=True)

        def _reap_done(fut: "asyncio.Future[int]") -> None:
            try:
                rc = fut.result()
            except Exception:  # noqa: BLE001 —— 收割线程任何异常都不阻断通知
                rc = -1
            _notify_terminal_exit(loop, conn_id, term_sid, rc)
            logger.info("ws.terminal.exit term_sid={} rc={}", term_sid, rc)

        try:
            loop.run_in_executor(None, _reap_unix_child,
                                 session["pid"]).add_done_callback(_reap_done)
        except RuntimeError:
            # loop 已关闭（停机竞态）：退化为就地收割，不再发通知
            _reap_unix_child(session["pid"])
            logger.info("ws.terminal.exit term_sid={} rc=-1 loop_closed",
                        term_sid)


def _handle_terminal_input(conn_id: str, msg: dict) -> None:
    """将用户输入写入终端 stdin。"""
    term_sid = str(msg.get("term_sid") or "")
    data = msg.get("data", "")
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if not session or not session["alive"]:
            return
        if session.get("conn_id") != conn_id:
            logger.warning("ws.terminal_input.denied conn_id={} owner={}", conn_id, session.get("conn_id"))
            return
        # 锁内获取引用，锁外做实际写入（避免阻塞其他会话）
        is_windows = session.get("is_windows")
        proc = session.get("proc")
        fd = session.get("fd")
    try:
        if is_windows:
            if session.get("conpty"):
                wp = session.get("winpty")
                if wp is not None:
                    wp.write(data)
            elif proc and proc.stdin:
                proc.stdin.write(data.encode("utf-8", errors="replace"))
                proc.stdin.flush()
        else:
            os.write(fd, data.encode("utf-8", errors="replace"))
    except (OSError, BrokenPipeError):
        logger.debug("ws.terminal_input_write_failed conn_id={}", conn_id, exc_info=True)


def _handle_terminal_resize(conn_id: str, msg: dict) -> None:
    """调整终端窗口大小。"""
    term_sid = str(msg.get("term_sid") or "")
    cols = int(msg.get("cols") or 80)
    rows = int(msg.get("rows") or 24)
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if not session or not session["alive"]:
            return
        if session.get("conn_id") != conn_id:
            logger.warning("ws.terminal_resize.denied conn_id={} owner={}", conn_id, session.get("conn_id"))
            return
        if session.get("is_windows"):
            # ConPTY 会话支持 resize；管道回退无 TTY 概念，no-op
            wp = session.get("winpty")
            if session.get("conpty") and wp is not None:
                try:
                    wp.resize(rows, cols)
                except (OSError, RuntimeError, ValueError):
                    logger.debug("ws.conpty_resize_failed term_sid={}", term_sid,
                                 exc_info=True)
            return
        fd = session.get("fd")
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        logger.debug("ws.terminal_resize_failed conn_id={}", conn_id, exc_info=True)


def _handle_terminal_kill(conn_id: str, msg: dict) -> None:
    """终止终端会话 (复用 _cleanup_pty 确保前端收到 terminal_exit)."""
    term_sid = str(msg.get("term_sid") or "")
    with _pty_sessions_lock:
        session = _pty_sessions.get(term_sid)
        if not session:
            return
        if session.get("conn_id") != conn_id:
            logger.warning("ws.terminal_kill.denied conn_id={} owner={}", conn_id, session.get("conn_id"))
            return
    _cleanup_pty(term_sid)
    logger.info("ws.terminal.kill term_sid={}", term_sid)
