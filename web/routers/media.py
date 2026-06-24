"""媒体工坊路由（R11）：TTS 同步合成（内容寻址缓存）、图/视频异步任务、画廊。"""
from __future__ import annotations

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


def _cfg():
    from web.config_service import get_config_service
    return get_config_service()


def _queue(request: Request):
    q = getattr(request.app.state, "media_queue", None)
    if not q:
        raise HTTPException(503, "媒体任务队列未启动")
    return q


# ── TTS ──────────────────────────────────────────────────────────


@router.post("/media/tts", response_model=Envelope[dict])
async def synthesize_tts(body: dict, request: Request):
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
async def tts_voices():
    from emotion.tts_engine import VOICE_REFERENCES, VOICE_STYLES, EMOTION_STYLE_MAP
    return Envelope(data={
        "voices": [{"id": v, "description": VOICE_STYLES.get(v, "")}
                   for v in VOICE_REFERENCES.keys()],
        "styles": sorted(EMOTION_STYLE_MAP.keys()),
    })


@router.get("/media/tts/config", response_model=Envelope[dict])
async def get_tts_config():
    cfg = _cfg()
    return Envelope(data={
        "auto_speak": cfg.get("tts.auto_speak", False),
        "default_voice": cfg.get("tts.default_voice", "nahida"),
    })


@router.put("/media/tts/config", response_model=Envelope[dict])
async def put_tts_config(body: dict, request: Request):
    cfg = _cfg()
    if "auto_speak" in body:
        cfg.set("tts.auto_speak", bool(body["auto_speak"]))
    if body.get("default_voice"):
        from emotion.tts_engine import VOICE_REFERENCES
        if body["default_voice"] not in VOICE_REFERENCES:
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
async def gen_image(body: dict, request: Request):
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    task_id = await _queue(request).submit("image", prompt, {
        "size": body.get("size", "1024x1024")})
    return Envelope(data={"task_id": task_id})


@router.post("/media/video", response_model=Envelope[dict])
async def gen_video(body: dict, request: Request):
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    task_id = await _queue(request).submit("video", prompt, {
        "seconds": body.get("seconds", 5)})
    return Envelope(data={"task_id": task_id})


@router.get("/media/tasks", response_model=Envelope[list[dict]])
async def list_tasks(request: Request, limit: int = Query(default=50, le=200)):
    return Envelope(data=await _queue(request).list(limit))


@router.get("/media/tasks/{task_id}", response_model=Envelope[dict])
async def get_task(task_id: str, request: Request):
    row = await _queue(request).get(task_id)
    if not row:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return Envelope(data=row)


@router.delete("/media/tasks/{task_id}", response_model=Envelope[dict])
async def cancel_task(task_id: str, request: Request):
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
                  limit: int = Query(default=24, le=100)):
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
async def delete_media(type: str, name: str, request: Request):
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
