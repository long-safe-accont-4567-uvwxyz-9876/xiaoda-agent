from __future__ import annotations
from typing import Any

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope, SystemStatus
from web.routers.auth import get_current_user

router = APIRouter(tags=["system"], dependencies=[Depends(get_current_user)])
# 公开路由（无需认证）：OS 信息不敏感，终端需在 token 未就绪/失效时也能正确探测服务端 OS，
# 否则前端会 fallback 到客户端 navigator 检测，导致用 Windows 浏览器访问 Linux 服务时误判为 Windows。
public_router = APIRouter(tags=["system"])

_start_time = time.time()


@public_router.get("/system/os", response_model=Envelope[dict])
async def get_server_os() -> Any:
    """返回服务器操作系统信息，供前端选择正确的 shell 类型。"""
    import platform
    system = platform.system().lower()  # linux / darwin / windows
    return Envelope(data={"os": system, "shell": "powershell" if system == "windows" else "bash"})


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
async def get_status(request: Request) -> Any:
    _core = request.app.state.core
    from security.permission_manager import get_permission_manager
    try:
        from web.ws_hub import manager as ws_manager
        active = ws_manager.active_count
    except Exception as exc:
        logger.debug("system.ws_manager_get_failed: {}", exc, exc_info=True)
        active = 0
    qq_connected = False
    try:
        # 基于真实 WebSocket 连接状态判断，而非“近10分钟有无 QQ 消息”。
        # 旧实现用 source='qq' 查询 conversation_logs，但 QQ 适配器实际写入的
        # source 是 'qq_c2c' / 'qq_group'，精确匹配永远查不到，导致恒为 False。
        # 且“消息活跃度”不等于“连接状态”——连上但没人发消息也会误显示离线。
        import qq_bot_adapter
        bot = qq_bot_adapter._ACTIVE_BOT
        if bot is not None and not bot.is_closed():
            qq_connected = True
    except Exception as exc:
        logger.debug("system.qq_status_check_failed: {}", exc, exc_info=True)
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
                    limit: int = Query(default=50, le=200)) -> Any:
    core = request.app.state.core
    cond, params = "1=1", []
    if event_type:
        cond = "event_type LIKE ?"
        params.append(f"%{event_type}%")
    rows = await core.db.fetch_all(
        f"SELECT * FROM audit_logs WHERE {cond} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (*tuple(params), limit, page * limit))
    return Envelope(data=rows)


@router.get("/system/metrics", response_model=Envelope[dict])
async def get_metrics() -> Any:
    from utils.metrics import metrics
    return Envelope(data=metrics.get_snapshot())


@router.get("/system/logs", response_model=Envelope[list[str]])
async def get_logs(lines: int = Query(default=200, le=1000),
                   level: str = Query(default="")) -> Any:
    try:
        from config import LOG_DIR
    except ImportError:
        LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "logs"

    out: list[str] = []

    # 优先读取 loguru 的 agent_YYYY-MM-DD.json 日志（同步 I/O 放到线程池）
    def _read_agent_logs() -> list[str]:
        import json as _json
        import datetime as _dt
        result: list[str] = []
        today = _dt.date.today().isoformat()
        agent_log = LOG_DIR / f"agent_{today}.json"
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
                rl = raw_line.strip()
                if not rl:
                    continue
                try:
                    obj = _json.loads(rl)
                    text = obj.get("text", "").rstrip("\n")
                    if not text:
                        continue
                    rec = obj.get("record", {})
                    lvl = rec.get("level", {}).get("name", "")
                    if level and lvl.upper() != level.upper():
                        continue
                    result.append(text)
                except _json.JSONDecodeError:
                    continue
        return result

    try:
        out = await asyncio.to_thread(_read_agent_logs)
    except Exception as exc:
        logger.debug("system.agent_log_read_failed: {}", exc, exc_info=True)

    # 兜底：如果 agent 日志为空，尝试读取 botpy.log
    if not out:
        def _read_botpy_log() -> list[str]:
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
                lines_out = content.splitlines()
                if level:
                    lines_out = [line for line in lines_out if f"| {level.upper()}" in line or f"[{level.upper()}]" in line]
                return lines_out
            return []
        try:
            out = await asyncio.to_thread(_read_botpy_log)
        except Exception as exc:
            logger.debug("system.botpy_log_read_failed: {}", exc, exc_info=True)

    if not out:
        return Envelope(data=["（暂无日志）"])
    return Envelope(data=out[-lines:])


@router.get("/system/lan-addresses", response_model=Envelope[dict])
async def get_lan_addresses(request: Request) -> Any:
    """返回局域网访问地址，供同一 WiFi 下手机访问。"""
    import socket
    # 从请求中获取实际运行端口，而非环境变量
    port = request.url.port or 8082
    # 获取本机局域网 IP
    lan_ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if primary_ip and not primary_ip.startswith("127."):
            lan_ips.append(primary_ip)
    except Exception as exc:
        logger.debug("system.lan_ip_detect_failed: {}", exc, exc_info=True)
    return Envelope(data={
        "localhost": f"http://localhost:{port}",
        "lan_ips": lan_ips,
        "lan_urls": [f"http://{ip}:{port}" for ip in lan_ips],
        "port": port,
    })


@router.get("/system/config", response_model=Envelope[dict])
async def get_config() -> Any:
    """合并后的 webui 配置（不含密钥）。"""
    from web.config_service import get_config_service
    return Envelope(data=get_config_service()._data)


@router.put("/system/config", response_model=Envelope[dict])
async def put_config(body: dict, request: Request) -> Any:
    """改 webui 顶层配置项，path 形如 'ui.particles'。"""
    from web.config_service import get_config_service
    path = body.get("path", "")
    if not path or any(seg in path for seg in ("api_key", "password", "secret")):
        raise HTTPException(400, "非法配置路径")
    _ALLOWED_PREFIXES = ("ui.", "tts.", "dashboard.", "mail.", "schedule.", "tools.", "mcp.", "models.")
    if not any(path.startswith(p) for p in _ALLOWED_PREFIXES):
        raise HTTPException(400, "不允许修改该配置路径")
    cfg = get_config_service()
    cfg.set(path, body.get("value"))
    core = request.app.state.core
    await core.db.insert_audit_log("webui.config.set", "webui",
                                   json.dumps({"path": path}, ensure_ascii=False))
    await core.db.commit()
    return Envelope(data={"path": path, "value": cfg.get(path)})


@router.get("/system/permission-mode", response_model=Envelope[dict])
async def get_permission_mode() -> Any:
    from security.permission_manager import get_permission_manager, PermissionMode
    return Envelope(data={
        "mode": get_permission_manager().mode.value,
        "options": [m.value for m in PermissionMode],
    })


@router.put("/system/permission-mode", response_model=Envelope[dict])
async def set_permission_mode(body: dict, request: Request) -> Any:
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
async def restart_service(request: Request) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    core = request.app.state.core
    await core.db.insert_audit_log("webui.system.restart", "webui", "user requested")
    await core.db.commit()
    import asyncio
    import sys
    import os

    is_windows = sys.platform == 'win32'

    async def _exit() -> None:
        await asyncio.sleep(1.0)
        if is_windows:
            # Windows: 创建延迟启动脚本，等旧进程退出后再启动
            import tempfile
            import subprocess
            import shlex
            python = sys.executable
            script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else 'agent.py'
            args = sys.argv[1:] if len(sys.argv) > 1 else ['--web', '--host', '0.0.0.0', '--port', '8082']
            bat_path = ""
            with tempfile.NamedTemporaryFile(suffix='.bat', delete=False, mode='w') as bat:
                safe_args = [shlex.quote(a) for a in args]
                bat.write('@echo off\ntimeout /t 2 /nobreak >nul\n"{}" "{}" {}\ndel "%~f0"\n'.format(python, script, ' '.join(safe_args)))
                bat_path = bat.name
            try:
                subprocess.Popen(['cmd', '/c', bat_path], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            except Exception as exc:
                os.unlink(bat_path)
                raise exc
            logger.warning("webui.restart.exiting (Windows auto-restart)")
        else:
            # Linux: 依赖 systemd 自动拉起
            logger.warning("webui.restart.exiting (systemd 将自动拉起)")
        os._exit(0)

    _exit_task = asyncio.create_task(_exit())
    return Envelope(data={"restarting": True, "platform": "windows" if is_windows else "linux"})


@router.get("/system/doctor", response_model=Envelope[dict])
async def run_doctor_check(fix: bool = Query(default=False, description="自动修复可修复的问题")) -> Any:
    from core.doctor import _create_default_doctor
    doc = _create_default_doctor()
    report = await asyncio.to_thread(doc.run, auto_fix=fix)
    return Envelope(data=report)


@router.post("/system/doctor/fix", response_model=Envelope[dict])
async def run_doctor_fix() -> Any:
    from core.doctor import _create_default_doctor
    doc = _create_default_doctor()
    report = await asyncio.to_thread(doc.run, auto_fix=True)
    return Envelope(data=report)
