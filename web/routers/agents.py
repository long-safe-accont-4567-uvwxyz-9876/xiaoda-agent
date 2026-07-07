from __future__ import annotations
from typing import Any

import base64
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["agents"])

_SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9_\u4e00-\u9fff-]+$')


def _validate_agent_name(name: str) -> str:
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400, "非法 Agent 名称")
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(400, "Agent 名称包含非法字符")
    return name


def _registry(request: Request) -> Any:
    return request.app.state.agent_registry


@router.get("/agents/public-wallpaper", response_model=Envelope[dict])
async def get_public_wallpaper(request: Request) -> Any:
    """无需认证的公开接口，返回主 Agent 壁纸（供登录页使用）。"""
    try:
        agents = _registry(request).list()
        main = next((a for a in agents if a.get("is_main")), None)
        if main and main.get("wallpaper"):
            return Envelope(data={"wallpaper": main["wallpaper"]})
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.debug("agents.public_wallpaper_failed: {}", e)
    return Envelope(data={"wallpaper": ""})


async def _audit(request: Request, action: str, detail: str) -> None:
    core = request.app.state.core
    try:
        await core.db.insert_audit_log(f"webui.agents.{action}", "webui", detail)
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        logger.debug("agents.audit_failed: {}", exc, exc_info=True)


@router.get("/agents", response_model=Envelope[list[dict]])
async def list_agents(request: Request, _user: str = Depends(get_current_user)) -> Any:
    return Envelope(data=_registry(request).list())


@router.get("/agents/{name}", response_model=Envelope[dict])
async def get_agent(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    data = _registry(request).get(name)
    if not data:
        raise HTTPException(404, f"Agent {name} 不存在")
    return Envelope(data=data)


@router.post("/agents", response_model=Envelope[dict])
async def create_agent(body: dict, request: Request, _user: str = Depends(get_current_user)) -> Any:
    try:
        data = await _registry(request).create(body)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    await _audit(request, "create", data["name"])
    return Envelope(data=data)


@router.put("/agents/{name}", response_model=Envelope[dict])
async def update_agent(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    try:
        data = await _registry(request).update(name, body)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit(request, "update", name)
    # display_name 变更时清除缓存并重新加载所有人格文件
    if "display_name" in body:
        from config import clear_display_name_cache
        clear_display_name_cache(name)
        # 重新加载所有子 Agent 的人格文件（因为人格文件中可能引用了被修改的 agent）
        try:
            core = request.app.state.core
            if core and hasattr(core, 'dispatcher') and hasattr(core.dispatcher, '_agents'):
                for agent_name, sub_agent in core.dispatcher._agents.items():
                    if hasattr(sub_agent, 'reload_personality'):
                        sub_agent.reload_personality()
        except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
            logger.debug("agents.reload_personality_failed: {}", exc, exc_info=True)
    # 通知所有标签页刷新（display_name 等变更需全局联动）
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "agents"})
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("agents.broadcast_failed: {}", exc, exc_info=True)
    return Envelope(data=data)


@router.delete("/agents/{name}", response_model=Envelope[dict])
async def delete_agent(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    try:
        await _registry(request).delete(name)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    await _audit(request, "delete", name)
    return Envelope(data={"deleted": name})


@router.post("/agents/{name}/enable", response_model=Envelope[dict])
async def enable_agent(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    try:
        _registry(request).set_enabled(name, True)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await _audit(request, "enable", name)
    return Envelope(data={"name": name, "enabled": True})


@router.post("/agents/{name}/disable", response_model=Envelope[dict])
async def disable_agent(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    try:
        _registry(request).set_enabled(name, False)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await _audit(request, "disable", name)
    return Envelope(data={"name": name, "enabled": False})


@router.get("/agents/{name}/permissions", response_model=Envelope[dict])
async def get_permissions(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    try:
        return Envelope(data=_registry(request).get_permissions(name))
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.put("/agents/{name}/permissions", response_model=Envelope[dict])
async def set_permissions(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    try:
        data = _registry(request).set_permissions(name, body)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit(request, "permissions",
                 json.dumps({"agent": name,
                             "tools_changed": len(body.get("tools") or {})},
                            ensure_ascii=False))
    # 通知所有标签页刷新
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "agents"})
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("agents.broadcast_failed: {}", exc, exc_info=True)
    return Envelope(data=data)


@router.get("/agents/{name}/personality", response_model=Envelope[dict])
async def get_personality(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    try:
        text = _registry(request).get_personality(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return Envelope(data={"name": name, "personality": text})


@router.put("/agents/{name}/personality", response_model=Envelope[dict])
async def set_personality(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    text = body.get("personality", "")
    # 主体小妲特殊处理：人格写入 SOUL.md，build_system_prompt 按 mtime 自动失效缓存
    if name == "xiaoda":
        from config import reverse_agent_name_replacements, WORKSPACE_DIR
        text = reverse_agent_name_replacements(text)
        soul_path = WORKSPACE_DIR / "SOUL.md"
        soul_path.write_text(text, encoding="utf-8-sig")
        await _audit(request, "personality", name)
        return Envelope(data={"name": name, "saved": True})
    try:
        await _registry(request).set_personality(name, text)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await _audit(request, "personality", name)
    return Envelope(data={"name": name, "saved": True})


@router.get("/agent-names", response_model=Envelope[dict])
async def get_agent_names(_user: str = Depends(get_current_user)) -> Any:
    """返回 agent 原名→显示名 映射表，供前端全局替换。

    覆盖三类名称：中文旧名（deprecated_names）、agent key。
    只使用中文显示名 (display_name)，不使用英文显示名。
    """
    from config import (
        get_all_deprecated_names,
        get_agent_display_name,
        agent_names,
    )

    def _best_display(agent_key: str) -> str | None:
        """返回 display_name。"""
        dn = get_agent_display_name(agent_key)
        return dn if dn else None

    mapping: dict[str, str] = {}

    # 旧名（deprecated_names） → 显示名
    for old_name, agent_key in get_all_deprecated_names().items():
        best = _best_display(agent_key)
        if best and best != old_name:
            mapping[old_name] = best

    # agent key → 显示名（如 xiaoda → 小花）
    for agent_key in agent_names():
        best = _best_display(agent_key)
        if best and best != agent_key:
            mapping[agent_key] = best

    return Envelope(data={"mapping": mapping})


# 壁纸目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    _WALLPAPER_DIR = MEDIA_DIR / "wallpapers"
except ImportError:
    _WALLPAPER_DIR = Path(__file__).resolve().parent.parent / "media" / "wallpapers"
_DATAURL_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,(.+)$", re.DOTALL)
_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp"}


@router.post("/agents/{name}/wallpaper", response_model=Envelope[dict])
async def upload_wallpaper(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    """上传背景板（data URL），保存后写入该 Agent 的 wallpaper 字段。
    
    每次上传生成带时间戳的新文件名，不覆盖旧文件，从根本上解决浏览器缓存问题。
    同时清理该 agent 的旧壁纸文件（仅保留最新一张）。
    """
    registry = _registry(request)
    if not registry.get(name):
        raise HTTPException(404, f"Agent {name} 不存在")
    m = _DATAURL_RE.match(body.get("data_url", ""))
    if not m:
        raise HTTPException(400, "仅支持 png/jpg/webp 的 data URL")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("agents.base64_decode_failed: {}", exc, exc_info=True)
        raise HTTPException(400, "base64 解码失败")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 8MB")
    _WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    # 生成带时间戳的新文件名，不覆盖旧文件 → 浏览器缓存自动失效
    ts = int(time.time())
    ext = _EXT[m.group(1).lower()]
    fp = _WALLPAPER_DIR / f"{name}_{ts}.{ext}"
    fp.write_bytes(raw)
    url = f"/media/wallpapers/{fp.name}"
    # 清理该 agent 的旧壁纸文件（保留最新一张，清理所有扩展名）
    try:
        for old in _WALLPAPER_DIR.glob(f"{name}_*.*"):
            if old != fp and old.suffix.lstrip(".") in _EXT:
                old.unlink(missing_ok=True)
        for old in _WALLPAPER_DIR.glob(f"{name}.*"):
            if old.suffix.lstrip(".") in _EXT:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    if name == "xiaoda":
        # 主体不在 dispatcher 中，壁纸持久化到 webui 配置
        from web.config_service import get_config_service
        try:
            get_config_service().set("ui.main_wallpaper", url)
        except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
            logger.warning("agents.wallpaper_config_save_failed: {}", exc)
            raise HTTPException(500, "壁纸配置保存失败，请检查磁盘空间") from exc
        from web.agent_registry import MAIN_AGENT_META
        MAIN_AGENT_META["wallpaper"] = url
    else:
        await registry.update(name, {"wallpaper": url})
    await _audit(request, "wallpaper", name)
    return Envelope(data={"name": name, "wallpaper": url})


@router.post("/agents/{name}/test", response_model=Envelope[dict])
async def test_agent(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    """对该 Agent 发一条固定测试语句。"""
    core = request.app.state.core
    t0 = time.time()
    test_msg = "请简短地自我介绍一下（30字以内）"
    try:
        if name == "xiaoda":
            # 绕过路由/委派逻辑，直接用小妲 system prompt 发起纯对话
            from config import build_system_prompt
            system_prompt = build_system_prompt()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": test_msg},
            ]
            result = await core.router.route("chat", messages, temperature=0.7)
            if isinstance(result, str):
                reply = core._clean_reply(result)
            else:
                reply = core._clean_reply(result.choices[0].message.content or "")
            ok = bool(reply and reply.strip())
        else:
            reply = await core.dispatcher.dispatch(name, test_msg)
            if not reply or not reply.strip():
                reply = "Agent 不可用（可能处于降级模式）"
                ok = False
            else:
                ok = True
        return Envelope(data={
            "ok": ok,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "reply": (reply or "")[:200],
        })
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("webui.agent_test_failed name={} error={}", name, str(e))
        return Envelope(data={"ok": False, "elapsed_ms": int((time.time() - t0) * 1000),
                              "reply": "", "error": str(e)[:200]})


# ── 表情包管理 ──────────────────────────────────────────

_EMOTION_CATEGORIES = [
    "happy", "excited", "love", "shy",
    "sad", "angry", "surprised", "confused",
    "thinking", "playful", "moved", "neutral",
    "pout", "fear", "anxious",
]
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _resolve_sticker_dir(name: str, request: Request) -> Path:
    """解析指定 Agent 的表情包目录路径。"""
    if name == "xiaoda":
        from config import STICKER_DIR
        return Path(STICKER_DIR)
    core = request.app.state.core
    # 从 _agent_route_configs 获取 sticker_dir
    route_cfg = getattr(core, "_agent_route_configs", {}).get(name, {})
    sticker_dir = route_cfg.get("sticker_dir", "")
    if sticker_dir:
        return Path(sticker_dir)
    # fallback: AGENT_STICKER_BASE / name
    from config import AGENT_STICKER_BASE
    return Path(AGENT_STICKER_BASE) / name


@router.get("/agents/{name}/stickers", response_model=Envelope[dict])
async def list_stickers(name: str, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    """列出指定 Agent 的所有表情包及描述。"""
    sticker_dir = _resolve_sticker_dir(name, request)
    if not sticker_dir.exists():
        return Envelope(data={"stickers": [], "emotions": _EMOTION_CATEGORIES})

    # 加载 descriptions.json
    desc_file = sticker_dir / "descriptions.json"
    descriptions: dict[str, str] = {}
    if desc_file.exists():
        try:
            descriptions = json.loads(desc_file.read_text(encoding="utf-8"))
        except (OSError, PermissionError, FileNotFoundError) as exc:
            logger.debug("agents.sticker_desc_parse_failed: {}", exc, exc_info=True)

    stickers = []
    for emo_dir in sorted(sticker_dir.iterdir()):
        if not emo_dir.is_dir():
            continue
        for f in sorted(emo_dir.iterdir()):
            if f.suffix.lower() not in _IMG_EXTS:
                continue
            desc = descriptions.get(f.name, f.stem)
            stickers.append({
                "name": f.name,
                "description": desc,
                "emotion": emo_dir.name,
                "url": f"/api/v1/agents/{name}/stickers/file/{f.name}",
            })

    return Envelope(data={"stickers": stickers, "emotions": _EMOTION_CATEGORIES})


@router.get("/agents/{name}/stickers/file/{filename}")
async def serve_sticker(name: str, filename: str, request: Request, token: str = "") -> Any:
    _validate_agent_name(name)
    """提供表情包图片文件。支持 query token 认证（img 标签无法发 header）。"""
    # 路径遍历防护
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    # 支持 header 或 query 参数认证
    from web.routers.auth import _validate_token
    auth_ok = False
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and _validate_token(auth_header[7:]):
        auth_ok = True
    elif token and _validate_token(token):
        auth_ok = True
    if not auth_ok:
        raise HTTPException(401, "未授权")
    from fastapi.responses import FileResponse
    sticker_dir = _resolve_sticker_dir(name, request)
    if not sticker_dir.exists():
        raise HTTPException(404, "表情包目录不存在")
    # 在所有情绪子目录中查找文件
    for emo_dir in sticker_dir.iterdir():
        if not emo_dir.is_dir():
            continue
        fp = emo_dir / filename
        if fp.exists() and fp.suffix.lower() in _IMG_EXTS:
            return FileResponse(str(fp), media_type="image/jpeg")
    raise HTTPException(404, f"表情包 {filename} 不存在")


@router.post("/agents/{name}/stickers", response_model=Envelope[dict])
async def upload_sticker(
    name: str, request: Request, _user: str = Depends(get_current_user)
) -> Any:
    _validate_agent_name(name)
    """上传表情包：multipart/form-data，字段 file (图片)、description (描述)、emotion (情绪分类)。"""
    from fastapi import UploadFile
    # 手动解析 multipart
    form = await request.form()
    file: UploadFile = form.get("file")  # type: ignore
    description = (form.get("description") or "").strip()
    emotion = (form.get("emotion") or "neutral").strip().lower()

    if not file:
        raise HTTPException(400, "缺少图片文件")
    if not description:
        raise HTTPException(400, "缺少表情包描述")
    if emotion not in _EMOTION_CATEGORIES:
        raise HTTPException(400, f"情绪分类必须是以下之一: {', '.join(_EMOTION_CATEGORIES)}")

    # 验证文件类型
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _IMG_EXTS:
        raise HTTPException(400, f"仅支持 {', '.join(_IMG_EXTS)} 格式")

    # 读取文件内容
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 8MB")

    # 确定保存路径
    sticker_dir = _resolve_sticker_dir(name, request)
    emo_dir = sticker_dir / emotion
    emo_dir.mkdir(parents=True, exist_ok=True)

    # 生成文件名: {emotion}_{描述}.ext (用时间戳避免冲突)
    import re as _re
    safe_desc = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', description)[:40]
    filename = f"{emotion}_{safe_desc}{ext}"
    fp = emo_dir / filename
    # 如果同名文件已存在，加时间戳
    if fp.exists():
        import time as _time
        filename = f"{emotion}_{safe_desc}_{int(_time.time())}{ext}"
        fp = emo_dir / filename

    fp.write_bytes(content)

    # 更新 descriptions.json
    desc_file = sticker_dir / "descriptions.json"
    descriptions: dict[str, str] = {}
    if desc_file.exists():
        try:
            descriptions = json.loads(desc_file.read_text(encoding="utf-8"))
        except (OSError, PermissionError, FileNotFoundError) as exc:
            logger.debug("agents.sticker_desc_parse_failed: {}", exc, exc_info=True)
    descriptions[filename] = description
    desc_file.write_text(json.dumps(descriptions, ensure_ascii=False, indent=2), encoding="utf-8")

    # 热重载 StickerManager 缓存
    try:
        core = request.app.state.core
        mgr = core.get_sticker_manager(name)
        if hasattr(mgr, "reload"):
            mgr.reload()
        elif hasattr(mgr, "_instance"):
            mgr._instance.reload()
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("agents.sticker_reload_failed: {}", exc, exc_info=True)

    await _audit(request, "sticker_upload", json.dumps({"agent": name, "file": filename}, ensure_ascii=False))

    return Envelope(data={
        "name": filename,
        "description": description,
        "emotion": emotion,
        "url": f"/api/v1/agents/{name}/stickers/file/{filename}",
    })


@router.delete("/agents/{name}/stickers/{filename:path}", response_model=Envelope[dict])
async def delete_sticker(
    name: str, filename: str, request: Request, _user: str = Depends(get_current_user)
) -> Any:
    _validate_agent_name(name)
    """删除指定表情包。"""
    # 路径遍历防护：禁止 .. 和路径分隔符
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    sticker_dir = _resolve_sticker_dir(name, request)
    if not sticker_dir.exists():
        raise HTTPException(404, "表情包目录不存在")

    # 查找并删除文件
    deleted = False
    for emo_dir in sticker_dir.iterdir():
        if not emo_dir.is_dir():
            continue
        fp = emo_dir / filename
        if fp.exists():
            fp.unlink()
            deleted = True
            break

    if not deleted:
        raise HTTPException(404, f"表情包 {filename} 不存在")

    # 从 descriptions.json 移除
    desc_file = sticker_dir / "descriptions.json"
    if desc_file.exists():
        try:
            descriptions = json.loads(desc_file.read_text(encoding="utf-8"))
            descriptions.pop(filename, None)
            desc_file.write_text(json.dumps(descriptions, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, PermissionError, FileNotFoundError) as exc:
            logger.debug("agents.sticker_desc_update_failed: {}", exc, exc_info=True)

    # 热重载缓存
    try:
        core = request.app.state.core
        mgr = core.get_sticker_manager(name)
        if hasattr(mgr, "reload"):
            mgr.reload()
        elif hasattr(mgr, "_instance"):
            mgr._instance.reload()
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("agents.sticker_reload_failed: {}", exc, exc_info=True)

    await _audit(request, "sticker_delete", json.dumps({"agent": name, "file": filename}, ensure_ascii=False))
    return Envelope(data={"deleted": filename})


@router.post("/agents/{name}/model", response_model=Envelope[dict])
async def set_agent_model(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)) -> Any:
    _validate_agent_name(name)
    """一键切换子 Agent 的模型。

    body: {"provider": str, "model_id": str}
    后端自动解析 base_url 和 api_key_env，热重载并持久化。
    """
    provider = (body.get("provider") or "").strip()
    model_id = (body.get("model_id") or "").strip()
    if not provider or not model_id:
        raise HTTPException(400, "provider 和 model_id 不能为空")
    try:
        data = await _registry(request).set_agent_model(name, provider, model_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit(request, "model",
                 json.dumps({"agent": name, "provider": provider, "model_id": model_id},
                            ensure_ascii=False))
    # 通知所有标签页刷新
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "agents"})
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("agents.broadcast_failed: {}", exc, exc_info=True)
    return Envelope(data=data)