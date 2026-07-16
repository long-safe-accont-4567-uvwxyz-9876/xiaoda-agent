"""WebSocket 主通道（§9 协议）：流式状态、工具事件、最终回复、问候/任务/配置广播。"""
from __future__ import annotations
from typing import Any

import asyncio
import json
import os
import platform
import shutil
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path


def _safe_int(val, default):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

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

from config import STREAM_STATUS_PUSH, STREAM_TEXT_PUSH, STREAM_TOOL_STATUS  # noqa: E402
from core.event_bus import event_bus  # noqa: E402
from agent_core.user_web import WebUser  # noqa: E402

router = APIRouter()

# 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    MEDIA_ROOT = MEDIA_DIR
except ImportError:
    MEDIA_ROOT = Path(__file__).resolve().parent / "media"


class ConnectionManager:
    """连接管理 + 事件广播。"""

    MAX_CONNECTIONS = 32  # 最大并发 WebSocket 连接数（防资源耗尽）

    def __init__(self) -> None:
        """初始化 WebSocket 连接管理器."""
        self._connections: dict[str, WebSocket] = {}
        self._agent_map: dict[str, str] = {}      # conn_id -> 当前受话 agent
        self._session_map: dict[str, str] = {}    # conn_id -> session_id
        self._tasks: dict[str, asyncio.Task] = {}  # msg_id -> 处理任务（abort 用）

    def register(self, ws: WebSocket) -> str:
        """注册一个新连接, 返回生成的连接 ID."""
        if len(self._connections) >= self.MAX_CONNECTIONS:
            raise ValueError(f"连接数已达上限 {self.MAX_CONNECTIONS}，拒绝新连接")
        conn_id = uuid.uuid4().hex[:8]
        self._connections[conn_id] = ws
        self._agent_map[conn_id] = "xiaoda"
        self._session_map[conn_id] = f"web_{uuid.uuid4().hex[:12]}"
        return conn_id

    def unregister(self, conn_id: str) -> None:
        """按连接 ID 注销连接及其会话映射."""
        self._connections.pop(conn_id, None)
        self._agent_map.pop(conn_id, None)
        self._session_map.pop(conn_id, None)

    async def send_to(self, conn_id: str, event: dict) -> None:
        """向指定连接发送事件, 失败则注销该连接."""
        ws = self._connections.get(conn_id)
        if ws:
            try:
                await ws.send_json(event)
            except (RuntimeError, OSError):
                logger.debug("ws.send_error conn_id={}", conn_id, exc_info=True)
                self.unregister(conn_id)

    async def broadcast(self, event: dict) -> None:
        """向所有活跃连接广播事件."""
        for conn_id in list(self._connections):
            await self.send_to(conn_id, event)

    @property
    def active_count(self) -> int:
        """返回当前活跃连接数."""
        return len(self._connections)


manager = ConnectionManager()

# PTY 终端会话: term_sid -> {pid, fd, conn_id, shell, alive}
_pty_sessions: dict[str, dict] = {}
_pty_sessions_lock = threading.Lock()


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
        try:
            if link:
                dest.symlink_to(src.resolve())
            else:
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
                pass
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
        _tts_bg = asyncio.create_task(_async_tts_task(
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
                                image_data: list[dict] | None = None) -> dict:
    """统一处理入口：主体走 AgentCore.process；子代理直达 dispatcher（R5）。

    斜杠命令（/ 开头）始终走主体 process（内部路由到 SlashCommandHandler）。
    Task 6: 当 TTS_ASYNC_MODE 开启且结果标记 tts_pending 时，启动后台合成任务。
    """
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
                except Exception:
                    pass
        else:
            # 走与 QQ 通道相同的完整子代理流程：表情包/情绪/TTS/落库都不缺
            from loguru import logger as _logger
            from agent_core import RequestContext
            from utils.trace_context import new_trace_id
            # 身份解析：与 core.process() 主路径一致，确保 is_master/user_openid 语义正确
            _identity = core._resolve_identity("webui", user_openid="", source="web")
            ctx = RequestContext(session_id=session_id, user_id="webui",
                                 user_input=text, status_callback=status_callback,
                                 is_master=_identity.is_owner)
            ctx.identity = _identity
            _tid = new_trace_id()
            trace = _logger.bind(trace_id=_tid)
            result = await core._dispatch_single_sub_agent(
                agent, text, user_id="webui", source="web",
                session_id=session_id, trace=trace, ctx=ctx)
            data = serialize_result(result)
            data["agent"] = agent
            data["elapsed_ms"] = int((time.time() - t0) * 1000)
            if app is not None and data.get("emotion"):
                app.state.last_emotion = {"primary": data["emotion"], "timestamp": time.time()}
            # Task 6: 异步 TTS —— WebSocket 走异步，HTTP 走同步回退
            await _resolve_pending_tts(core, agent, result, data, conn_id, msg_id)
            return data
    else:
        result = await core.process(
            user_input=text, user_id="webui", source="web",
            session_id=session_id, status_callback=status_callback,
            image_data=image_data)
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
async def websocket_endpoint(ws: WebSocket, token: str = "") -> None:
    # 先验证 token 再 accept，防止无 token 连接耗尽资源
    from web.routers.auth import _validate_token
    if not token or not _validate_token(token):
        await ws.close(code=1008, reason="Unauthorized")
        return

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

            if mtype == "ping":
                await manager.send_to(conn_id, {"type": "pong"})

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
                manager._tasks[msg_id] = task
                task.add_done_callback(lambda _t, m=msg_id: manager._tasks.pop(m, None))

            elif mtype == "terminal_start":
                term_sid = str(msg.get("term_sid") or uuid.uuid4().hex[:8])
                _t = asyncio.create_task(_handle_terminal_start(conn_id, msg, term_sid))

                def _on_term_start_done(t):
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
                task = manager._tasks.get(str(msg.get("msg_id") or ""))
                if task and not task.done():
                    task.cancel()

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
        manager.unregister(conn_id)


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


async def _handle_chat(conn_id: str, msg: dict, msg_id: str, ws: WebSocket) -> None:
    text = (msg.get("text") or "").strip()
    if not text:
        return
    agent = str(msg.get("agent") or manager._agent_map.get(conn_id, "xiaoda"))
    session_id = str(msg.get("session_id") or
                     manager._session_map.get(conn_id) or f"web_{uuid.uuid4().hex[:12]}")
    manager._session_map[conn_id] = session_id
    app = ws.scope.get("app")
    core = app.state.core

    from web._msg_context import current_msg_id
    token = current_msg_id.set(msg_id)

    # 从文本中提取 [Image: URL] 标记，构建 image_data
    image_data = None
    import re as _re
    _image_urls = _re.findall(r'\[Image:\s*([^\]]+)\]', text)
    if _image_urls:
        from pathlib import Path as _Path
        from utils.text_utils import encode_image_to_base64
        image_data = []
        for url in _image_urls:
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

    # Task 7: 流式状态推送回调 —— 受 STREAM_STATUS_PUSH 开关控制
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
            })
            return
        if STREAM_STATUS_PUSH:
            await manager.send_to(conn_id, {
                "type": "status", "msg_id": msg_id,
                "stage": "thinking", "text": str(message)[:200],
            })

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
                image_data=image_data)
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
        current_msg_id.reset(token)


async def _handle_terminal_start(conn_id: str, msg: dict, term_sid: str) -> None:
    """启动一个终端会话：Linux 用 PTY，Windows 用 subprocess 管道。

    msg 字段：
      shell    — Shell 类型 (bash/zsh/python/node/cmd/powershell/wsl)，默认 bash
      cols     — 终端列数
      rows     — 终端行数
    """
    shell_type = (msg.get("shell") or "bash").strip().lower()
    cols = int(msg.get("cols") or 80)
    rows = int(msg.get("rows") or 24)

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
        # ── Windows: subprocess + 管道 ──
        shell_map_win = {
            "cmd": ["cmd.exe"],
            "powershell": ["powershell.exe", "-NoLogo"],
            "pwsh": ["pwsh.exe", "-NoLogo"],
            "python": ["python.exe"],
            "node": ["node.exe"],
            "wsl": ["wsl.exe"],
            "bash": ["bash.exe"],
        }
        cmd = shell_map_win.get(shell_type, ["cmd.exe"])
        loop = asyncio.get_running_loop()

        try:
            proc = _subprocess.Popen(
                cmd,
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
                    "is_windows": True,
                }
            logger.info("ws.terminal.start term_sid={} shell={} pid={}", term_sid, shell_type, proc.pid)
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

        # 将输出推送到前端（用户实时看到）
        loop.call_soon(asyncio.ensure_future, manager.send_to(conn_id, {
            "type": "terminal_output", "term_sid": term_sid, "data": text}))

        # 送入标记符检测器（内部按行缓冲）
        try:
            from web.pty_executor import feed_output
            feed_output(text)
        except (ImportError, OSError, RuntimeError):
            logger.debug("ws.feed_output_error", exc_info=True)

    loop.add_reader(fd, _on_pty_readable)


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
                loop.call_soon_threadsafe(asyncio.ensure_future, manager.send_to(conn_id, {
                    "type": "terminal_output", "term_sid": term_sid, "data": text}))

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


def _cleanup_pty(term_sid: str) -> None:
    """清理终端会话（在 reader 回调中调用，不能 await）。"""
    with _pty_sessions_lock:
        session = _pty_sessions.pop(term_sid, None)
    if not session:
        return
    session["alive"] = False
    conn_id = session["conn_id"]
    loop: asyncio.AbstractEventLoop = session["loop"]
    is_win = session.get("is_windows", False)

    if is_win:
        # ── Windows: 关闭 subprocess ──
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
    else:
        # ── Unix: 关闭 PTY fd + 等待子进程 ──
        fd = session["fd"]
        try:
            loop.remove_reader(fd)
        except (OSError, ValueError):
            logger.debug("ws.remove_reader_error", exc_info=True)
        try:
            _, status = os.waitpid(session["pid"], os.WNOHANG)
            rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        except (OSError, ChildProcessError):
            logger.debug("ws.waitpid_error", exc_info=True)
            rc = -1
        try:
            os.close(fd)
        except OSError:
            logger.debug("ws.close_fd_error", exc_info=True)

    try:
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                manager.send_to(conn_id, {
                    "type": "terminal_exit", "term_sid": term_sid, "returncode": rc
                }), loop=loop))
    except RuntimeError:
        logger.debug("ws.terminal_exit_send_failed term_sid={}", term_sid)
    logger.info("ws.terminal.exit term_sid={} rc={}", term_sid, rc)


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
            if proc and proc.stdin:
                proc.stdin.write(data.encode("utf-8", errors="replace"))
                proc.stdin.flush()
        else:
            os.write(fd, data.encode("utf-8", errors="replace"))
    except (OSError, BrokenPipeError):
        pass


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
            return  # Windows subprocess 不支持 resize
        fd = session.get("fd")
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


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