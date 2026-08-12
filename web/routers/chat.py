from __future__ import annotations
from typing import Any

import asyncio
import json
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
from emotion.emotion_simple import detect_emotion

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
MAX_DOC_SIZE = 20 * 1024 * 1024  # 20MB
_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# P0 新增（Task 1.9）：文档上传支持 — 与图片上传分离
# 根因：原 upload-image 端点仅接受 image/*，文档（PDF/DOCX 等）无法上传。
#       用户说"上传文件不是只上传图片"，文档应走 document_reader 工具，而非 vision API。
_ALLOWED_DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".md"}


def _strip_tags(text: str) -> str:
    return _EMOTION_TAG.sub("", text or "").strip()


def decode_history_context(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or set(value) != {"text", "search", "think", "attachments"}:
        return None
    if not isinstance(value["text"], str):
        return None
    if not isinstance(value["search"], bool) or not isinstance(value["think"], bool):
        return None
    if not isinstance(value["attachments"], list):
        return None
    upload_root = UPLOAD_DIR.resolve()
    for attachment in value["attachments"]:
        if not isinstance(attachment, dict):
            return None
        kind = attachment.get("kind")
        allowed_keys = {"kind", "url", "name"} if kind == "image" else {"kind", "url", "name", "path", "ext"}
        required_keys = {"kind", "url", "name"} if kind == "image" else {"kind", "url", "name", "path"}
        if kind not in {"image", "document"} or not required_keys <= set(attachment) <= allowed_keys:
            return None
        name = attachment.get("name")
        url = attachment.get("url")
        if not isinstance(name, str) or not name or len(name) > 255 or Path(name).name != name:
            return None
        if not isinstance(url, str) or url != f"/media/upload/{Path(url).name}":
            return None
        local_path = (upload_root / Path(url).name).resolve()
        if local_path.parent != upload_root or not local_path.is_file():
            return None
        if kind == "document":
            path = attachment.get("path")
            if not isinstance(path, str) or Path(path).resolve() != local_path:
                return None
            ext = attachment.get("ext")
            if ext is not None and (not isinstance(ext, str) or ext != local_path.suffix.lower()):
                return None
    return value


def _infer_emotion(text: str) -> dict:
    """从文本推断情绪，返回含 emotion/intensity 的字典；异常时返回空字典。"""
    try:
        result = detect_emotion(text)
        return {
            "emotion": result.get("primary", "平静"),
            "intensity": result.get("intensity", 0.0),
        }
    except Exception:
        logger.debug("chat.emotion_inference_failed", exc_info=True)
        return {}


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
            "   WHERE COALESCE(NULLIF(cl2.session_id, ''), cl2.user_id) = cl.session_id "
            "   ORDER BY cl2.timestamp ASC LIMIT 1) AS first_message, "
            "  (SELECT assistant_reply FROM conversation_logs cl3 "
            "   WHERE COALESCE(NULLIF(cl3.session_id, ''), cl3.user_id) = cl.session_id "
            "   ORDER BY cl3.timestamp DESC LIMIT 1) AS last_reply "
            "FROM ("
            "  SELECT COALESCE(NULLIF(session_id, ''), user_id) AS session_id, "
            "    COUNT(*) AS cnt, MIN(timestamp) AS created, "
            "    MAX(timestamp) AS updated, MIN(source) AS source "
            "  FROM conversation_logs "
            "  WHERE session_id != '' OR user_id != '' "
            "  GROUP BY COALESCE(NULLIF(session_id, ''), user_id) "
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
        # 治本修复：空 session_id 的记录用 user_id 匹配（微信 bot 不传 session_id）
        cond = "COALESCE(NULLIF(session_id, ''), user_id)=?"
        params: tuple = (session_id,)
        if before:
            cond += " AND timestamp<?"
            params = (session_id, before)
        rows = await core.db.fetch_all(
            f"SELECT id, timestamp, user_message, assistant_reply, emotion_label, request_context_json "
            f"FROM conversation_logs WHERE {cond} ORDER BY timestamp DESC LIMIT ?",
            (*params, limit))
        for row in reversed(rows):
            if row["user_message"]:
                messages.append(MessageItem(
                    id=row["id"] * 2, role="user", content=row["user_message"],
                    emotion=None, timestamp=row["timestamp"],
                    request_context=decode_history_context(row["request_context_json"])))
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


@router.post("/sessions/{session_id}/export")
async def export_session(session_id: str, request: Request) -> Any:
    # POST + Authorization header 安全下载（token 不再通过 URL 暴露）
    # 兼容历史 <a href> 调用：仍允许 query token
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
        if not _validate_token(token):
            raise HTTPException(401, "Invalid or expired token")
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("chat.validate_token_failed: {}", exc, exc_info=True)
        raise HTTPException(401, "Invalid or expired token") from None
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
    ext = Path(file.filename or "image.png").suffix.lower() or ".png"
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, f"不支持的图片格式，仅允许 {', '.join(sorted(_ALLOWED_IMAGE_EXTS))}")
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(content)
    return Envelope(data={"url": f"/media/upload/{filename}", "name": filename})


# P0 新增（Task 1.9）：文档上传端点 — 与图片上传分离
# 根因：用户说"上传文件不是只上传图片"。文档（PDF/DOCX 等）应走 document_reader 工具，
#       而非 vision API。前端上传文档时调用此端点，后端返回路径，
#       LLM 通过 document_reader 工具读取内容。
@router.post("/chat/upload-doc", response_model=Envelope[dict])
async def upload_doc(file: UploadFile = File(...)) -> Any:
    """上传文档文件（PDF/DOCX/PPTX/XLSX/TXT/MD）。

    与 upload-image 分离：文档不走 vision API，而是返回路径供 document_reader 工具读取。
    """
    content = await file.read()
    if len(content) > MAX_DOC_SIZE:
        raise HTTPException(400, "文档大小不能超过 20MB")
    ext = Path(file.filename or "doc.pdf").suffix.lower() or ".pdf"
    if ext not in _ALLOWED_DOC_EXTS:
        raise HTTPException(400, f"不支持的文档格式，仅允许 {', '.join(sorted(_ALLOWED_DOC_EXTS))}")
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(content)
    abs_path = str(dest.resolve())
    logger.info("chat.doc_uploaded name={} size={} path={}", filename, len(content), abs_path)
    return Envelope(data={
        "url": f"/media/upload/{filename}",
        "name": filename,
        "path": abs_path,  # 供 document_reader 工具使用的绝对路径
        "ext": ext,
    })


@router.post("/chat/speech-to-text", response_model=Envelope[dict])
async def speech_to_text(file: UploadFile = File(...)) -> Any:
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:  # 20MB
        raise HTTPException(400, "音频大小不能超过 20MB")

    # 功能节点后端控制：asr=off 时禁用语音识别
    try:
        from web.config_service import get_config_service
        from web.local_deploy_nodes import get_backend
        if get_backend(get_config_service(), "asr") == "off":
            raise HTTPException(503, "ASR 已关闭（语音识别节点设置为关闭）")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass

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
            return Envelope(data={"text": text, **_infer_emotion(text)})

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
        return Envelope(data={"text": text, **_infer_emotion(text)})

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"ASR 不可用：{e!s}") from None
