from __future__ import annotations
from typing import Any

import asyncio
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from loguru import logger

from web.schemas import Envelope, ChatRequest, SessionInfo, MessageItem, SlashCommand
from web.routers.auth import get_current_user

router = APIRouter(tags=["chat"], dependencies=[Depends(get_current_user)])

_EMOTION_TAG = re.compile(r"\[emotion:[^\]]*\]")

# 上传目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    UPLOAD_DIR = MEDIA_DIR / "upload"
except ImportError:
    UPLOAD_DIR = Path(__file__).resolve().parent.parent / "media" / "upload"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB


def _strip_tags(text: str) -> str:
    return _EMOTION_TAG.sub("", text or "").strip()


@router.get("/commands", response_model=Envelope[list[SlashCommand]])
async def list_commands() -> Any:
    """斜杠命令清单（供前端 / 自动补全）。"""
    from slash_commands import list_commands as _list
    return Envelope(data=[SlashCommand(**c) for c in _list()])


@router.get("/sessions", response_model=Envelope[list[SessionInfo]])
async def list_sessions(request: Request) -> Any:
    core = request.app.state.core
    sessions = []
    try:
        # 跨通道会话：web / qq / cli 同库展示（同一个 AgentCore 进程写入）
        # 使用关联子查询在单次 SQL 中获取首次/末次消息，避免 N+1 查询
        rows = await core.db.fetch_all(
            "SELECT cl.session_id, cl.cnt, cl.created, cl.updated, cl.source, "
            "  (SELECT user_message FROM conversation_logs cl2 "
            "   WHERE cl2.session_id = cl.session_id "
            "   ORDER BY cl2.timestamp ASC LIMIT 1) AS first_message, "
            "  (SELECT assistant_reply FROM conversation_logs cl3 "
            "   WHERE cl3.session_id = cl.session_id "
            "   ORDER BY cl3.timestamp DESC LIMIT 1) AS last_reply "
            "FROM ("
            "  SELECT session_id, COUNT(*) AS cnt, MIN(timestamp) AS created, "
            "    MAX(timestamp) AS updated, MIN(source) AS source "
            "  FROM conversation_logs "
            "  WHERE session_id != '' "
            "  GROUP BY session_id "
            "  ORDER BY updated DESC "
            "  LIMIT 50"
            ") cl")
        for row in rows:
            sid = row["session_id"]
            src = (row["source"] or "web").split("_")[0]  # qq_c2c/qq_group → qq
            sessions.append(SessionInfo(
                session_id=sid,
                title=_strip_tags(row["first_message"] or sid)[:50],
                last_message=_strip_tags(row["last_reply"] or "")[:80],
                message_count=row["cnt"] * 2,
                created_at=row["created"] or 0,
                updated_at=row["updated"] or 0,
                source=src,
            ))
    except Exception as e:
        logger.warning("webui.sessions.list_failed error={}", str(e))
    return Envelope(data=sessions)


@router.post("/sessions", response_model=Envelope[dict])
async def create_session() -> Any:
    return Envelope(data={"session_id": f"web_{uuid.uuid4().hex[:12]}"})


@router.get("/sessions/{session_id}/messages", response_model=Envelope[list[MessageItem]])
async def get_messages(session_id: str, request: Request,
                       before: float = Query(default=0),
                       limit: int = Query(default=50, le=200)) -> Any:
    """conversation_logs 一行 = 一轮（user_message + assistant_reply），展开为两条消息。"""
    core = request.app.state.core
    messages: list[MessageItem] = []
    try:
        cond = "session_id=?"
        params: tuple = (session_id,)
        if before:
            cond += " AND timestamp<?"
            params = (session_id, before)
        rows = await core.db.fetch_all(
            f"SELECT id, timestamp, user_message, assistant_reply, emotion_label "
            f"FROM conversation_logs WHERE {cond} ORDER BY timestamp DESC LIMIT ?",
            params + (limit,))
        for row in reversed(rows):
            if row["user_message"]:
                messages.append(MessageItem(
                    id=row["id"] * 2, role="user", content=row["user_message"],
                    emotion=None, timestamp=row["timestamp"]))
            if row["assistant_reply"]:
                messages.append(MessageItem(
                    id=row["id"] * 2 + 1, role="assistant",
                    content=_strip_tags(row["assistant_reply"]),
                    emotion=row["emotion_label"] or None, timestamp=row["timestamp"]))
    except Exception as e:
        logger.warning("webui.messages.list_failed error={}", str(e))
    return Envelope(data=messages)


@router.delete("/sessions/{session_id}", response_model=Envelope[dict])
async def delete_session(session_id: str, request: Request) -> Any:
    core = request.app.state.core
    await core.db.execute(
        "DELETE FROM conversation_logs WHERE session_id=?", (session_id,))
    await core.db.insert_audit_log("webui.session.delete", "webui", session_id)
    await core.db.commit()
    return Envelope(data={"deleted": session_id})


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, request: Request) -> Any:
    # 支持 query token（用于 <a href> 直接下载）或 header token
    token = request.query_params.get("token") or ""
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Missing or invalid Authorization header")
    # 验证 token
    try:
        from web.routers.auth import _validate_token
        _validate_token(token)
    except Exception as exc:
        logger.debug("chat.validate_token_failed: {}", exc, exc_info=True)
        raise HTTPException(401, "Invalid or expired token")
    core = request.app.state.core
    rows = await core.db.fetch_all(
        "SELECT timestamp, user_message, assistant_reply FROM conversation_logs "
        "WHERE session_id=? ORDER BY timestamp ASC", (session_id,))
    address_term = getattr(core.context, "current_address_term", "") or "爸爸"
    agent_name = getattr(core.context, "current_agent_name", "") or "小妲"
    lines = [f"# 对话导出 · {session_id}", ""]
    for row in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["timestamp"]))
        if row["user_message"]:
            lines.append(f"**{address_term}** ({ts})：\n\n{row['user_message']}\n")
        if row["assistant_reply"]:
            lines.append(f"**{agent_name}** ({ts})：\n\n{_strip_tags(row['assistant_reply'])}\n")
    return PlainTextResponse(
        "\n".join(lines), media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={session_id}.md"})


@router.post("/chat", response_model=Envelope[dict])
async def chat(req: ChatRequest, request: Request) -> Any:
    """非流式兜底端点（主通道为 /ws）。"""
    core = request.app.state.core
    try:
        from web.ws_hub import process_and_serialize
        data = await process_and_serialize(
            core, req.text, session_id=req.session_id or f"web_{uuid.uuid4().hex[:12]}",
            agent=req.agent, app=request.app)
        return Envelope(data=data)
    except Exception as e:
        logger.error("webui.chat.failed error={}", str(e))
        return Envelope(ok=False, error={"code": "CHAT_ERROR", "message": str(e)})


@router.post("/chat/upload-image", response_model=Envelope[dict])
async def upload_image(file: UploadFile = File(...)) -> Any:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "仅允许上传图片文件")
    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(400, "图片大小不能超过 10MB")
    ext = Path(file.filename or "image.png").suffix or ".png"
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(content)
    return Envelope(data={"url": f"/media/upload/{filename}", "name": filename})


@router.post("/chat/speech-to-text", response_model=Envelope[dict])
async def speech_to_text(file: UploadFile = File(...)) -> Any:
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:  # 20MB
        raise HTTPException(400, "音频大小不能超过 20MB")

    try:
        from config import ASR_API_KEY, ASR_BASE_URL, ASR_MODEL
        if not ASR_API_KEY:
            # 降级：尝试使用 MIMO ASR（向后兼容）
            mimo_key = os.getenv("MIMO_API_KEY", "")
            if not mimo_key:
                raise HTTPException(503, "ASR 不可用：未配置 SILICONFLOW_API_KEY 或 MIMO_API_KEY")
            # MIMO 降级路径 — sync OpenAI SDK 调用放到线程池
            def _mimo_asr() -> str:
                from openai import OpenAI
                client = OpenAI(api_key=mimo_key, base_url=os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"))
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp.write(content)
                        tmp_path = tmp.name
                    with open(tmp_path, "rb") as audio_file:
                        transcript = client.audio.transcriptions.create(model="mimo-v2.5-asr", file=audio_file)
                    return transcript.text
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
            text = await asyncio.to_thread(_mimo_asr)
            return Envelope(data={"text": text})

        # 主路径：SiliconFlow + TeleSpeechASR — sync OpenAI SDK 调用放到线程池
        def _siliconflow_asr() -> str:
            from openai import OpenAI
            client = OpenAI(api_key=ASR_API_KEY, base_url=ASR_BASE_URL)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                with open(tmp_path, "rb") as audio_file:
                    transcript = client.audio.transcriptions.create(model=ASR_MODEL, file=audio_file)
                return transcript.text if hasattr(transcript, "text") else str(transcript)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        text = await asyncio.to_thread(_siliconflow_asr)
        # 如果返回的是 JSON 字符串，尝试解析提取 text 字段
        if text.startswith("{") and '"text"' in text:
            import json as _json
            try:
                text = _json.loads(text).get("text", text)
            except Exception as exc:
                logger.debug("chat.json_parse_failed: {}", exc, exc_info=True)
        return Envelope(data={"text": text})

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"ASR 不可用：{e!s}")
