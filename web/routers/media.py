"""媒体工坊路由（R11）：TTS 同步合成（内容寻址缓存）、图/视频异步任务、画廊。"""
from __future__ import annotations
from typing import Any

import hashlib
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["media"], dependencies=[Depends(get_current_user)])

# 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    MEDIA_ROOT = MEDIA_DIR
except ImportError:
    MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"


def _cfg() -> Any:
    from web.config_service import get_config_service
    return get_config_service()


def _queue(request: Request) -> Any:
    q = getattr(request.app.state, "media_queue", None)
    if not q:
        raise HTTPException(503, "媒体任务队列未启动")
    return q


# ── TTS ──────────────────────────────────────────────────────────


@router.post("/media/tts", response_model=Envelope[dict])
async def synthesize_tts(body: dict, request: Request) -> Any:
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    if len(text) > 500:
        raise HTTPException(400, "text 最长 500 字")
    voice = body.get("voice") or _cfg().get("tts.default_voice", "nahida")
    style = body.get("style", "")
    core = request.app.state.core
    if not core.tts.available:
        raise HTTPException(503, "TTS 引擎不可用（检查 MIMO_API_KEY 与参考音频）")

    # 内容寻址缓存
    digest = hashlib.sha1(f"{text}|{voice}|{style}".encode()).hexdigest()[:20]
    tts_dir = MEDIA_ROOT / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    cached = next(iter(tts_dir.glob(f"{digest}.*")), None)
    if cached:
        logger.info("webui.tts.cache_hit digest={}", digest)
        return Envelope(data={"audio_url": f"/media/tts/{cached.name}", "cached": True})

    t0 = time.time()
    path = await core.tts.synthesize(text, voice=voice, style=style)
    if not path or not Path(path).exists():
        raise HTTPException(500, "TTS 合成失败（详见后端日志）")
    src = Path(path)
    dest = tts_dir / f"{digest}{src.suffix or '.wav'}"
    shutil.copy2(str(src), str(dest))
    return Envelope(data={
        "audio_url": f"/media/tts/{dest.name}",
        "cached": False,
        "elapsed_ms": int((time.time() - t0) * 1000),
    })


@router.get("/media/tts/voices", response_model=Envelope[dict])
async def tts_voices() -> Any:
    from emotion.tts_engine import list_all_voices, EMOTION_STYLE_MAP
    all_voices = list_all_voices()
    # 转为按 agent 分组: {agent: [{name, voice_ref}]}
    groups = {}
    for agent, voices in all_voices.items():
        groups[agent] = [{"name": v["name"], "voice_ref": f"{agent}/{v['name']}"} for v in voices]
    return Envelope(data={
        "groups": groups,
        "styles": sorted(EMOTION_STYLE_MAP.keys()),
    })


@router.post("/media/tts/voices/{agent}", response_model=Envelope[dict])
async def upload_voice_ref(agent: str, request: Request) -> Any:
    """上传指定 agent 的参考音频。FormData: name=音色名, file=音频文件(.mp3/.wav, <10MB)。"""
    from emotion.tts_engine import get_agent_voice_dir
    import re
    form = await request.form()
    name = (form.get("name") or "").strip()
    file = form.get("file")
    if not name or not file:
        raise HTTPException(400, "name 和 file 不能为空")
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(400, "音色名只允许字母、数字、下划线、中横线")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "音频文件不能超过 10MB")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".mp3", ".wav"):
        raise HTTPException(400, "仅支持 .mp3 和 .wav 格式")
    agent_dir = get_agent_voice_dir(agent)
    # 删除同名旧文件
    for f in agent_dir.iterdir():
        if f.stem == name:
            f.unlink()
    dest = agent_dir / f"{name}{ext}"
    dest.write_bytes(content)
    logger.info("media.voice_ref_uploaded agent={} name={} size={}", agent, name, len(content))
    return Envelope(data={"name": name, "voice_ref": f"{agent}/{name}", "path": str(dest)})


@router.delete("/media/tts/voices/{agent}/{name}", response_model=Envelope[dict])
async def delete_voice_ref(agent: str, name: str) -> Any:
    """删除指定 agent 的参考音频。"""
    from emotion.tts_engine import get_agent_voice_dir
    agent_dir = get_agent_voice_dir(agent)
    deleted = False
    for f in agent_dir.iterdir():
        if f.stem == name:
            f.unlink()
            deleted = True
    if not deleted:
        raise HTTPException(404, f"音色 {name} 不存在")
    logger.info("media.voice_ref_deleted agent={} name={}", agent, name)
    return Envelope(data={"deleted": name})


@router.get("/media/tts/config", response_model=Envelope[dict])
async def get_tts_config() -> Any:
    cfg = _cfg()
    return Envelope(data={
        "auto_speak": cfg.get("tts.auto_speak", False),
        "default_voice": cfg.get("tts.default_voice", "nahida"),
    })


@router.put("/media/tts/config", response_model=Envelope[dict])
async def put_tts_config(body: dict, request: Request) -> Any:
    cfg = _cfg()
    if "auto_speak" in body:
        cfg.set("tts.auto_speak", bool(body["auto_speak"]))
    if body.get("default_voice"):
        from emotion.tts_engine import resolve_voice_path
        if resolve_voice_path(body["default_voice"]) is None:
            raise HTTPException(400, f"未知音色 {body['default_voice']}")
        cfg.set("tts.default_voice", body["default_voice"])
    core = request.app.state.core
    await core.db.insert_audit_log("webui.media.tts_config", "webui", str(body))
    await core.db.commit()
    return Envelope(data={
        "auto_speak": cfg.get("tts.auto_speak"),
        "default_voice": cfg.get("tts.default_voice"),
    })


# ── 图片 / 视频任务 ──────────────────────────────────────────────


@router.post("/media/image", response_model=Envelope[dict])
async def gen_image(body: dict, request: Request) -> Any:
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    task_id = await _queue(request).submit("image", prompt, {
        "size": body.get("size", "1024x1024")})
    return Envelope(data={"task_id": task_id})


@router.post("/media/video", response_model=Envelope[dict])
async def gen_video(body: dict, request: Request) -> Any:
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    task_id = await _queue(request).submit("video", prompt, {
        "seconds": body.get("seconds", 5)})
    return Envelope(data={"task_id": task_id})


@router.get("/media/tasks", response_model=Envelope[list[dict]])
async def list_tasks(request: Request, limit: int = Query(default=50, le=200)) -> Any:
    return Envelope(data=await _queue(request).list(limit))


@router.get("/media/tasks/{task_id}", response_model=Envelope[dict])
async def get_task(task_id: str, request: Request) -> Any:
    row = await _queue(request).get(task_id)
    if not row:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return Envelope(data=row)


@router.delete("/media/tasks/{task_id}", response_model=Envelope[dict])
async def cancel_task(task_id: str, request: Request) -> Any:
    ok = await _queue(request).cancel(task_id)
    if not ok:
        raise HTTPException(400, "仅 queued 状态的任务可取消")
    return Envelope(data={"cancelled": task_id})


# ── 画廊 ─────────────────────────────────────────────────────────

_GALLERY_EXTS = {"image": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
                 "video": (".mp4", ".webm"),
                 "audio": (".wav", ".mp3", ".flac")}
_GALLERY_DIRS = {"image": "image", "video": "video", "audio": "tts"}


@router.get("/media/gallery", response_model=Envelope[list[dict]])
async def gallery(type: str = Query(default="image"),
                  page: int = Query(default=0, ge=0),
                  limit: int = Query(default=24, le=100)) -> Any:
    if type not in _GALLERY_DIRS:
        raise HTTPException(400, "type 必须是 image/video/audio")
    d = MEDIA_ROOT / _GALLERY_DIRS[type]
    if not d.exists():
        return Envelope(data=[])
    files = [f for f in d.iterdir()
             if f.is_file() and f.suffix.lower() in _GALLERY_EXTS[type]]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    window = files[page * limit:(page + 1) * limit]
    return Envelope(data=[
        {"name": f.name, "url": f"/media/{_GALLERY_DIRS[type]}/{f.name}",
         "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        for f in window])


@router.delete("/media/gallery/{type}/{name}", response_model=Envelope[dict])
async def delete_media(type: str, name: str, request: Request) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    if type not in _GALLERY_DIRS or "/" in name or ".." in name:
        raise HTTPException(400, "非法参数")
    fp = (MEDIA_ROOT / _GALLERY_DIRS[type] / name).resolve()
    if not str(fp).startswith(str(MEDIA_ROOT.resolve())) or not fp.exists():
        raise HTTPException(404, "文件不存在")
    fp.unlink()
    core = request.app.state.core
    await core.db.insert_audit_log("webui.media.delete", "webui", f"{type}/{name}")
    await core.db.commit()
    return Envelope(data={"deleted": name})
