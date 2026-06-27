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


def _read_version() -> str:
    """读取安装包版本号，从 .version 文件获取"""
    version = "dev"
    from pathlib import Path
    import sys

    # PyInstaller onedir 模式：.version 在可执行文件同目录
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent.parent

    for candidate in [
        base / ".version",
        base / "_internal" / ".version",
    ]:
        if candidate.exists():
            version = candidate.read_text(encoding="utf-8").strip() or "dev"
            break
    return version


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
        version=_read_version(),
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
    try:
        from config import LOG_DIR
    except ImportError:
        LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "logs"

    out: list[str] = []

    # 优先读取 loguru 的 agent_YYYY-MM-DD.json 日志
    try:
        import json as _json
        import datetime as _dt
        today = _dt.date.today().isoformat()
        agent_log = LOG_DIR / f"agent_{today}.json"
        # 如果今天的日志不存在，找最近的一个
        if not agent_log.exists():
            candidates = sorted(LOG_DIR.glob("agent_*.json"), reverse=True)
            if candidates:
                agent_log = candidates[0]
        if agent_log.exists():
            with open(agent_log, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 512 * 1024))
                content = f.read().decode("utf-8", errors="replace")
            for raw_line in content.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = _json.loads(raw_line)
                    # loguru serialize 格式：text 字段包含完整格式化文本
                    text = obj.get("text", "").rstrip("\n")
                    if not text:
                        continue
                    # 按级别过滤
                    rec = obj.get("record", {})
                    lvl = rec.get("level", {}).get("name", "")
                    if level and lvl.upper() != level.upper():
                        continue
                    out.append(text)
                except _json.JSONDecodeError:
                    continue
    except Exception:
        pass

    # 兜底：如果 agent 日志为空，尝试读取 botpy.log
    if not out:
        try:
            from config import get_base_dir
            log_path = LOG_DIR / "botpy.log"
            if not log_path.exists():
                log_path = get_base_dir() / "botpy.log"
            if log_path.exists():
                with open(log_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 512 * 1024))
                    content = f.read().decode("utf-8", errors="replace")
                out = content.splitlines()
                if level:
                    out = [l for l in out if f"| {level.upper()}" in l or f"[{level.upper()}]" in l]
        except Exception:
            pass

    if not out:
        return Envelope(data=["（暂无日志）"])
    return Envelope(data=out[-lines:])


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
        "options": [m.value for m in PermissionMode],
    })


@router.put("/system/permission-mode", response_model=Envelope[dict])
async def set_permission_mode(body: dict, request: Request):
    mode = (body.get("mode") or "").lower()
    confirm = body.get("confirm", "").lower()
    from security.permission_manager import get_permission_manager, PermissionMode
    valid = {m.value for m in PermissionMode}
    if mode not in valid:
        raise HTTPException(400, f"未知模式 {mode}")
    # GOAT 模式需要二次确认
    if mode == "goat" and confirm != "yes":
        raise HTTPException(400, "梭哈模式需要二次确认，请传入 confirm: yes")
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
