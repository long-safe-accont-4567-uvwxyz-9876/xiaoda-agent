from __future__ import annotations

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


def _registry(request: Request):
    return request.app.state.agent_registry


async def _audit(request: Request, action: str, detail: str):
    core = request.app.state.core
    try:
        await core.db.insert_audit_log(f"webui.agents.{action}", "webui", detail)
        await core.db.commit()
    except Exception:
        pass


@router.get("/agents", response_model=Envelope[list[dict]])
async def list_agents(request: Request):
    return Envelope(data=_registry(request).list())


@router.get("/agents/{name}", response_model=Envelope[dict])
async def get_agent(name: str, request: Request, _user: str = Depends(get_current_user)):
    data = _registry(request).get(name)
    if not data:
        raise HTTPException(404, f"Agent {name} 不存在")
    return Envelope(data=data)


@router.post("/agents", response_model=Envelope[dict])
async def create_agent(body: dict, request: Request, _user: str = Depends(get_current_user)):
    try:
        data = await _registry(request).create(body)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    await _audit(request, "create", data["name"])
    return Envelope(data=data)


@router.put("/agents/{name}", response_model=Envelope[dict])
async def update_agent(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)):
    try:
        data = await _registry(request).update(name, body)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit(request, "update", name)
    return Envelope(data=data)


@router.delete("/agents/{name}", response_model=Envelope[dict])
async def delete_agent(name: str, request: Request, _user: str = Depends(get_current_user)):
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    try:
        await _registry(request).delete(name)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    await _audit(request, "delete", name)
    return Envelope(data={"deleted": name})


@router.post("/agents/{name}/enable", response_model=Envelope[dict])
async def enable_agent(name: str, request: Request, _user: str = Depends(get_current_user)):
    try:
        _registry(request).set_enabled(name, True)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await _audit(request, "enable", name)
    return Envelope(data={"name": name, "enabled": True})


@router.post("/agents/{name}/disable", response_model=Envelope[dict])
async def disable_agent(name: str, request: Request, _user: str = Depends(get_current_user)):
    try:
        _registry(request).set_enabled(name, False)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await _audit(request, "disable", name)
    return Envelope(data={"name": name, "enabled": False})


@router.get("/agents/{name}/permissions", response_model=Envelope[dict])
async def get_permissions(name: str, request: Request, _user: str = Depends(get_current_user)):
    try:
        return Envelope(data=_registry(request).get_permissions(name))
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.put("/agents/{name}/permissions", response_model=Envelope[dict])
async def set_permissions(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)):
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
    except Exception:
        pass
    return Envelope(data=data)


@router.get("/agents/{name}/personality", response_model=Envelope[dict])
async def get_personality(name: str, request: Request, _user: str = Depends(get_current_user)):
    try:
        text = _registry(request).get_personality(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return Envelope(data={"name": name, "personality": text})


@router.put("/agents/{name}/personality", response_model=Envelope[dict])
async def set_personality(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)):
    text = body.get("personality", "")
    try:
        await _registry(request).set_personality(name, text)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await _audit(request, "personality", name)
    return Envelope(data={"name": name, "saved": True})


_WALLPAPER_DIR = Path(__file__).resolve().parent.parent / "media" / "wallpapers"
_DATAURL_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,(.+)$", re.DOTALL)
_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp"}


@router.post("/agents/{name}/wallpaper", response_model=Envelope[dict])
async def upload_wallpaper(name: str, body: dict, request: Request, _user: str = Depends(get_current_user)):
    """上传背景板（data URL），保存后写入该 Agent 的 wallpaper 字段。"""
    registry = _registry(request)
    if not registry.get(name):
        raise HTTPException(404, f"Agent {name} 不存在")
    m = _DATAURL_RE.match(body.get("data_url", ""))
    if not m:
        raise HTTPException(400, "仅支持 png/jpg/webp 的 data URL")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except Exception:
        raise HTTPException(400, "base64 解码失败")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 8MB")
    _WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    fp = _WALLPAPER_DIR / f"{name}.{_EXT[m.group(1).lower()]}"
    fp.write_bytes(raw)
    url = f"/media/wallpapers/{fp.name}?v={int(time.time())}"
    if name == "nahida":
        # 主体不在 dispatcher 中，壁纸持久化到 webui 配置
        from web.config_service import get_config_service
        get_config_service().set("ui.main_wallpaper", url)
        from web.agent_registry import MAIN_AGENT_META
        MAIN_AGENT_META["wallpaper"] = url
    else:
        await registry.update(name, {"wallpaper": url})
    await _audit(request, "wallpaper", name)
    return Envelope(data={"name": name, "wallpaper": url})


@router.post("/agents/{name}/test", response_model=Envelope[dict])
async def test_agent(name: str, request: Request, _user: str = Depends(get_current_user)):
    """对该 Agent 发一条固定测试语句。"""
    core = request.app.state.core
    t0 = time.time()
    test_msg = "请简短地自我介绍一下（30字以内）"
    try:
        if name == "nahida":
            # 绕过路由/委派逻辑，直接用纳西妲 system prompt 发起纯对话
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
    except Exception as e:
        logger.warning("webui.agent_test_failed name={} error={}", name, str(e))
        return Envelope(data={"ok": False, "elapsed_ms": int((time.time() - t0) * 1000),
                              "reply": "", "error": str(e)[:200]})
