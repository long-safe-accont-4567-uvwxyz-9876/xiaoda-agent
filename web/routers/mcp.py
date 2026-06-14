"""MCP 服务路由（R6）：server CRUD、生命周期控制、工具发现。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["mcp"], dependencies=[Depends(get_current_user)])


def _cfg():
    from web.config_service import get_config_service
    return get_config_service()


def _manager(request: Request):
    return request.app.state.core._mcp_manager


async def _audit(request: Request, action: str, detail: str):
    core = request.app.state.core
    try:
        await core.db.insert_audit_log(f"webui.mcp.{action}", "webui", detail)
        await core.db.commit()
    except Exception:
        pass


async def _broadcast_changed():
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "mcp"})
    except Exception:
        pass


def _serialize(name: str, client, cfg_record: dict | None) -> dict:
    record = cfg_record or {}
    if client:
        status = "running" if client.available else "stopped"
        tool_names = sorted(client.tool_names)
        command, args = client.command, client.args
        env_keys = sorted((client.env or {}).keys())
    else:
        status = "stopped"
        tool_names = []
        command = record.get("command", "")
        args = record.get("args", [])
        env_keys = sorted((record.get("env") or {}).keys())
    return {
        "name": name,
        "command": command,
        "args": args,
        "env_keys": env_keys,
        "status": status,
        "tool_names": tool_names,
        "managed_by_webui": cfg_record is not None,
        "last_error": getattr(client, "last_error", "") if client else "",
    }


@router.get("/mcp/servers", response_model=Envelope[list[dict]])
async def list_servers(request: Request):
    mgr = _manager(request)
    custom = _cfg().get("mcp", {}) or {}
    names = set(mgr._clients.keys()) | set(custom.keys())
    return Envelope(data=[
        _serialize(n, mgr._clients.get(n), custom.get(n)) for n in sorted(names)])


async def start_server(request: Request, name: str, record: dict) -> dict:
    """启动（或重启）一个 MCP server 并发现工具。"""
    from tool_engine.mcp_client import MCPClient
    mgr = _manager(request)
    old = mgr._clients.get(name)
    if old:
        try:
            await old.stop()
        except Exception:
            pass
    client = MCPClient(name, record.get("command", ""),
                       record.get("args", []), record.get("env") or None)
    mgr._clients[name] = client
    await client.start()
    return _serialize(name, client, record)


@router.post("/mcp/servers", response_model=Envelope[dict])
async def create_server(body: dict, request: Request):
    name = (body.get("name") or "").strip()
    command = (body.get("command") or "").strip()
    if not name or not name.replace("-", "_").isidentifier():
        raise HTTPException(400, "name 必须是合法标识符")
    if not command:
        raise HTTPException(400, "command 不能为空")
    cfg = _cfg()
    mgr = _manager(request)
    if name in mgr._clients or cfg.get(f"mcp.{name}"):
        raise HTTPException(400, f"MCP server {name} 已存在")
    record = {
        "command": command,
        "args": [str(a) for a in (body.get("args") or [])],
        "env": {str(k): str(v) for k, v in (body.get("env") or {}).items()},
        "enabled": True,
    }
    cfg.set(f"mcp.{name}", record)
    try:
        data = await start_server(request, name, record)
    except Exception as e:
        logger.warning("webui.mcp.start_failed name={} error={}", name, str(e))
        data = _serialize(name, None, record)
        data["status"] = "error"
        data["last_error"] = str(e)[:300]
    await _audit(request, "create", name)
    await _broadcast_changed()
    return Envelope(data=data)


@router.put("/mcp/servers/{name}", response_model=Envelope[dict])
async def update_server(name: str, body: dict, request: Request):
    cfg = _cfg()
    record = cfg.get(f"mcp.{name}")
    if not record:
        raise HTTPException(404, f"MCP server {name} 不是 WebUI 管理的（或不存在）")
    for f in ("command", "args", "env", "enabled"):
        if f in body and body[f] is not None:
            record[f] = body[f]
    cfg.set(f"mcp.{name}", record)
    mgr = _manager(request)
    client = mgr._clients.get(name)
    if client:
        try:
            await client.stop()
        except Exception:
            pass
        mgr._clients.pop(name, None)
    data = _serialize(name, None, record)
    if record.get("enabled", True):
        try:
            data = await start_server(request, name, record)
        except Exception as e:
            data["status"] = "error"
            data["last_error"] = str(e)[:300]
    await _audit(request, "update", name)
    await _broadcast_changed()
    return Envelope(data=data)


@router.delete("/mcp/servers/{name}", response_model=Envelope[dict])
async def delete_server(name: str, request: Request):
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    cfg = _cfg()
    if not cfg.get(f"mcp.{name}"):
        raise HTTPException(404, f"MCP server {name} 不是 WebUI 管理的（或不存在）")
    mgr = _manager(request)
    client = mgr._clients.pop(name, None)
    if client:
        try:
            await client.stop()
        except Exception:
            pass
    cfg.delete(f"mcp.{name}")
    await _audit(request, "delete", name)
    await _broadcast_changed()
    return Envelope(data={"deleted": name})


@router.post("/mcp/servers/{name}/start", response_model=Envelope[dict])
@router.post("/mcp/servers/{name}/restart", response_model=Envelope[dict])
async def restart_server(name: str, request: Request):
    cfg = _cfg()
    record = cfg.get(f"mcp.{name}")
    mgr = _manager(request)
    if not record:
        client = mgr._clients.get(name)
        if not client:
            raise HTTPException(404, f"MCP server {name} 不存在")
        record = {"command": client.command, "args": client.args, "env": client.env}
    try:
        data = await start_server(request, name, record)
    except Exception as e:
        raise HTTPException(500, f"启动失败：{str(e)[:300]}")
    await _audit(request, "start", name)
    await _broadcast_changed()
    return Envelope(data=data)


@router.post("/mcp/servers/{name}/stop", response_model=Envelope[dict])
async def stop_server(name: str, request: Request):
    mgr = _manager(request)
    client = mgr._clients.get(name)
    if not client:
        raise HTTPException(404, f"MCP server {name} 未运行")
    await client.stop()
    await _audit(request, "stop", name)
    await _broadcast_changed()
    return Envelope(data={"name": name, "status": "stopped"})


@router.get("/mcp/servers/{name}/tools", response_model=Envelope[list[str]])
async def server_tools(name: str, request: Request):
    client = _manager(request)._clients.get(name)
    if not client:
        raise HTTPException(404, f"MCP server {name} 不存在")
    return Envelope(data=sorted(client.tool_names))


# ── MCP Templates ──

MCP_TEMPLATES = [
    {
        "name": "filesystem",
        "description": "文件系统访问",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
    },
    {
        "name": "github",
        "description": "GitHub API",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env_vars": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    },
    {
        "name": "brave-search",
        "description": "Brave 搜索",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env_vars": ["BRAVE_API_KEY"],
    },
    {
        "name": "sqlite",
        "description": "SQLite 数据库",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "/path/to/db"],
    },
    {
        "name": "fetch",
        "description": "HTTP 请求",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    },
]


@router.get("/mcp/templates", response_model=Envelope[list[dict]])
async def get_mcp_templates():
    """获取 MCP 服务器模板列表"""
    return Envelope(data=MCP_TEMPLATES)


# ── MCP Health ──

@router.get("/mcp/servers/{server_name}/health", response_model=Envelope[dict])
async def get_mcp_health(server_name: str, request: Request):
    """获取 MCP 服务器健康状态"""
    mgr = _manager(request)
    client = mgr._clients.get(server_name)
    if not client:
        raise HTTPException(404, f"Server '{server_name}' not found")
    return Envelope(data={
        "server": server_name,
        "connected": client._connected,
        "available": client.available,
        "transport": client._config.transport if hasattr(client, "_config") else "stdio",
    })


# ── Tool-level Permissions ──

class ToolEnabledRequest(BaseModel):
    enabled: bool


@router.put("/mcp/servers/{server_name}/tools/{tool_name}/enabled", response_model=Envelope[dict])
async def set_tool_enabled(server_name: str, tool_name: str, req: ToolEnabledRequest, request: Request):
    """设置工具级启用/禁用"""
    mgr = _manager(request)
    mgr.set_tool_enabled(server_name, tool_name, req.enabled)
    return Envelope(data={"status": "ok", "server": server_name, "tool": tool_name, "enabled": req.enabled})
