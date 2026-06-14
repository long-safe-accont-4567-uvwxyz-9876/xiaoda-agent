from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope, SystemStatus
from web.routers.auth import get_current_user

router = APIRouter(tags=["system"], dependencies=[Depends(get_current_user)])

_start_time = time.time()


@router.get("/system/status", response_model=Envelope[SystemStatus])
async def get_status(request: Request):
    core = request.app.state.core
    from security.permission_manager import get_permission_manager
    try:
        from web.ws_hub import manager as ws_manager
        active = ws_manager.active_count
    except Exception:
        active = 0
    qq_connected = False
    try:
        rows = await core.db.fetch_all(
            "SELECT COUNT(*) AS c FROM conversation_logs "
            "WHERE source='qq' AND timestamp > ?", (time.time() - 600,))
        qq_connected = bool(rows and rows[0]["c"] > 0)
    except Exception:
        pass
    return Envelope(data=SystemStatus(
        uptime=time.time() - _start_time,
        qq_connected=qq_connected,
        active_sessions=active,
        version="1.0.0",
        permission_mode=get_permission_manager().mode.value,
    ))


@router.get("/system/audit", response_model=Envelope[list[dict]])
async def get_audit(request: Request,
                    event_type: str = Query(default=""),
                    page: int = Query(default=0, ge=0),
                    limit: int = Query(default=50, le=200)):
    core = request.app.state.core
    cond, params = "1=1", []
    if event_type:
        cond = "event_type LIKE ?"
        params.append(f"%{event_type}%")
    rows = await core.db.fetch_all(
        f"SELECT * FROM audit_logs WHERE {cond} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, page * limit))
    return Envelope(data=rows)


@router.get("/system/metrics", response_model=Envelope[dict])
async def get_metrics():
    from utils.metrics import metrics
    return Envelope(data=metrics.get_snapshot())


@router.get("/system/logs", response_model=Envelope[list[str]])
async def get_logs(lines: int = Query(default=200, le=1000),
                   level: str = Query(default="")):
    log_path = Path(__file__).resolve().parent.parent.parent / "botpy.log"
    if not log_path.exists():
        return Envelope(data=["（日志文件不存在）"])
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            content = f.read().decode("utf-8", errors="replace")
        out = content.splitlines()
        if level:
            out = [l for l in out if f"| {level.upper()}" in l or f"[{level.upper()}]" in l]
        return Envelope(data=out[-lines:])
    except Exception as e:
        return Envelope(ok=False, error={"code": "LOG_READ_ERROR", "message": str(e)})


@router.get("/system/config", response_model=Envelope[dict])
async def get_config():
    """合并后的 webui 配置（不含密钥）。"""
    from web.config_service import get_config_service
    return Envelope(data=get_config_service()._data)


@router.put("/system/config", response_model=Envelope[dict])
async def put_config(body: dict, request: Request):
    """改 webui 顶层配置项，path 形如 'ui.particles'。"""
    from web.config_service import get_config_service
    path = body.get("path", "")
    if not path or any(seg in path for seg in ("api_key", "password", "secret")):
        raise HTTPException(400, "非法配置路径")
    cfg = get_config_service()
    cfg.set(path, body.get("value"))
    core = request.app.state.core
    await core.db.insert_audit_log("webui.config.set", "webui",
                                   json.dumps({"path": path}, ensure_ascii=False))
    await core.db.commit()
    return Envelope(data={"path": path, "value": cfg.get(path)})


@router.get("/system/permission-mode", response_model=Envelope[dict])
async def get_permission_mode():
    from security.permission_manager import get_permission_manager, PermissionMode
    return Envelope(data={
        "mode": get_permission_manager().mode.value,
        "options": [m.value for m in PermissionMode if m.value != "bypass"],
    })


@router.put("/system/permission-mode", response_model=Envelope[dict])
async def set_permission_mode(body: dict, request: Request):
    mode = (body.get("mode") or "").lower()
    if mode == "bypass":
        raise HTTPException(400, "UI 禁止设置 BYPASS 模式")
    from security.permission_manager import get_permission_manager, PermissionMode
    valid = {m.value for m in PermissionMode}
    if mode not in valid:
        raise HTTPException(400, f"未知模式 {mode}")
    get_permission_manager().set_mode(mode)
    core = request.app.state.core
    await core.db.insert_audit_log("webui.permission_mode.set", "webui", mode)
    await core.db.commit()
    return Envelope(data={"mode": mode})


@router.post("/system/restart", response_model=Envelope[dict])
async def restart_service(request: Request):
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    core = request.app.state.core
    await core.db.insert_audit_log("webui.system.restart", "webui", "user requested")
    await core.db.commit()
    import asyncio
    import sys

    async def _exit():
        await asyncio.sleep(1.0)
        logger.warning("webui.restart.exiting (systemd 将自动拉起)")
        sys.exit(0)

    asyncio.create_task(_exit())
    return Envelope(data={"restarting": True})
