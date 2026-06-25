"""WebSocket 主通道（§9 协议）：流式状态、工具事件、最终回复、问候/任务/配置广播。"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from config import TTS_ASYNC_MODE, STREAM_STATUS_PUSH, STREAM_TEXT_PUSH, STREAM_TOOL_STATUS

router = APIRouter()

# 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    MEDIA_ROOT = MEDIA_DIR
except ImportError:
    MEDIA_ROOT = Path(__file__).resolve().parent / "media"


class ConnectionManager:
    """连接管理 + 事件广播。"""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._agent_map: dict[str, str] = {}      # conn_id -> 当前受话 agent
        self._session_map: dict[str, str] = {}    # conn_id -> session_id
        self._tasks: dict[str, asyncio.Task] = {}  # msg_id -> 处理任务（abort 用）

    def register(self, ws: WebSocket) -> str:
        conn_id = uuid.uuid4().hex[:8]
        self._connections[conn_id] = ws
        self._agent_map[conn_id] = "nahida"
        self._session_map[conn_id] = f"web_{uuid.uuid4().hex[:12]}"
        return conn_id

    def unregister(self, conn_id: str):
        self._connections.pop(conn_id, None)
        self._agent_map.pop(conn_id, None)
        self._session_map.pop(conn_id, None)

    async def send_to(self, conn_id: str, event: dict):
        ws = self._connections.get(conn_id)
        if ws:
            try:
                await ws.send_json(event)
            except Exception:
                self.unregister(conn_id)

    async def broadcast(self, event: dict):
        for conn_id in list(self._connections):
            await self.send_to(conn_id, event)

    @property
    def active_count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


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


def serialize_result(result) -> dict:
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


async def _async_tts_task(core, agent: str, tts_text: str, emotion: str,
                           conn_id: str, msg_id: str):
    """Task 6: 后台 TTS 合成任务 —— 合成完成后推送 audio_ready 事件。"""
    try:
        if agent == "nahida":
            audio_path = await core.tts.synthesize_nahida(tts_text, emotion=emotion)
        else:
            sub_agent = core.dispatcher.get_agent(agent)
            if sub_agent:
                audio_path = await sub_agent.synthesize(tts_text, emotion=emotion)
            else:
                audio_path = await core.tts.synthesize_nahida(tts_text, emotion=emotion)

        audio_url = _publish_file(audio_path, "tts") if audio_path else None
        if audio_url:
            await manager.send_to(conn_id, {
                "type": "audio_ready", "msg_id": msg_id, "audio_url": audio_url
            })
        else:
            logger.warning("ws.async_tts_no_audio", conn_id=conn_id, msg_id=msg_id)
    except Exception as e:
        logger.error("ws.async_tts_failed", conn_id=conn_id, msg_id=msg_id, error=str(e))


async def _synthesize_tts_sync(core, agent: str, tts_text: str, emotion: str) -> str | None:
    """同步 TTS 合成（HTTP 端点等无 WebSocket 连接场景的回退）。"""
    try:
        if agent == "nahida":
            audio_path = await core.tts.synthesize_nahida(tts_text, emotion=emotion)
        else:
            sub_agent = core.dispatcher.get_agent(agent)
            if sub_agent:
                audio_path = await sub_agent.synthesize(tts_text, emotion=emotion)
            else:
                audio_path = await core.tts.synthesize_nahida(tts_text, emotion=emotion)
        return _publish_file(audio_path, "tts") if audio_path else None
    except Exception as e:
        logger.error("ws.sync_tts_failed", error=str(e))
        return None


async def _resolve_pending_tts(core, agent: str, result, data: dict,
                                conn_id: str, msg_id: str):
    """Task 6: 处理 tts_pending 结果 —— WebSocket 走异步，HTTP 走同步回退。"""
    if not getattr(result, "tts_pending", False):
        return
    if conn_id and msg_id:
        # WebSocket：启动后台合成任务，先返回 audio_pending
        data["audio_pending"] = True
        asyncio.create_task(_async_tts_task(
            core, agent, result.tts_text, result.emotion, conn_id, msg_id))
    else:
        # HTTP 端点等无 WS 连接：同步合成回退
        audio_url = await _synthesize_tts_sync(
            core, agent, result.tts_text, result.emotion)
        if audio_url:
            data["audio_url"] = audio_url


async def process_and_serialize(core, text: str, session_id: str,
                                agent: str = "nahida",
                                status_callback=None, app=None,
                                conn_id: str = "", msg_id: str = "",
                                image_data: list[dict] | None = None) -> dict:
    """统一处理入口：主体走 AgentCore.process；子代理直达 dispatcher（R5）。

    斜杠命令（/ 开头）始终走主体 process（内部路由到 SlashCommandHandler）。
    Task 6: 当 TTS_ASYNC_MODE 开启且结果标记 tts_pending 时，启动后台合成任务。
    """
    t0 = time.time()
    if agent != "nahida" and not text.strip().startswith("/"):
        registry = getattr(app.state, "agent_registry", None) if app else None
        if registry and not registry.is_enabled(agent):
            raise ValueError(f"Agent {agent} 已被禁用")
        if not core.dispatcher.get_agent(agent):
            # 降级模式：子 Agent 未注册时回退到主 Agent
            from loguru import logger as _logger
            _logger.warning("ws.agent_fallback agent={} msg='not registered, falling back to nahida'", agent)
            agent = "nahida"
        else:
            # 走与 QQ 通道相同的完整子代理流程：表情包/情绪/TTS/落库都不缺
            from loguru import logger as _logger
            from agent_core import RequestContext
            ctx = RequestContext(session_id=session_id, user_id="webui",
                                 user_input=text, status_callback=status_callback)
            trace = _logger.bind(trace_id=f"web{int(time.time()*1000) % 1000000:06d}")
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
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    # 先 accept 再验证，避免 403
    await ws.accept()

    from web.routers.auth import _validate_token
    if not token or not _validate_token(token):
        await ws.send_json({"type": "error", "code": "UNAUTHORIZED", "message": "Invalid or missing token"})
        await ws.close(code=4001, reason="Unauthorized")
        return
    conn_id = manager.register(ws)
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
                agent = str(msg.get("agent") or "nahida")
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

            elif mtype == "abort":
                task = manager._tasks.get(str(msg.get("msg_id") or ""))
                if task and not task.done():
                    task.cancel()

    except WebSocketDisconnect:
        logger.info("ws.disconnected conn_id={}", conn_id)
    except Exception as e:
        logger.error("ws.error conn_id={} error={}", conn_id, str(e))
    finally:
        manager.unregister(conn_id)


async def _handle_chat(conn_id: str, msg: dict, msg_id: str, ws: WebSocket):
    text = (msg.get("text") or "").strip()
    if not text:
        return
    agent = str(msg.get("agent") or manager._agent_map.get(conn_id, "nahida"))
    session_id = str(msg.get("session_id") or
                     manager._session_map.get(conn_id) or f"web_{uuid.uuid4().hex[:12]}")
    manager._session_map[conn_id] = session_id
    app = ws.scope.get("app")
    core = app.state.core

    from web.tool_events import current_msg_id
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
                    image_data.append({"mimeType": mime, "data": img_b64})
                    logger.info("ws.image_loaded url={} size={}KB", url, len(img_b64) // 1024)
                else:
                    logger.warning("ws.image_not_found url={} path={}", url, local_path)
            except Exception as e:
                logger.warning("ws.image_load_failed url={} error={}", url, str(e))

    # Task 7: 流式状态推送回调 —— 受 STREAM_STATUS_PUSH 开关控制
    async def on_status(message):
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
        if STREAM_STATUS_PUSH:
            await manager.send_to(conn_id, {"type": "status", "msg_id": msg_id, "stage": "thinking"})
        data = await process_and_serialize(
            core, text, session_id=session_id, agent=agent,
            status_callback=on_status, app=app,
            conn_id=conn_id, msg_id=msg_id,
            image_data=image_data)
        data.update({"type": "final", "msg_id": msg_id})
        await manager.send_to(conn_id, data)
    except asyncio.CancelledError:
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "ABORTED", "message": "已中断生成"})
    except Exception as e:
        logger.error("ws.chat.failed conn_id={} error={}", conn_id, str(e))
        await manager.send_to(conn_id, {
            "type": "error", "msg_id": msg_id,
            "code": "CHAT_ERROR", "message": str(e)[:300]})
    finally:
        current_msg_id.reset(token)
