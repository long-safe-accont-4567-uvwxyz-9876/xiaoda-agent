"""插件管理 REST API"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Any

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(prefix="/plugins", tags=["plugins"],
                   dependencies=[Depends(get_current_user)])


class PluginSummary(BaseModel):
    id: str
    name: str
    version: str
    description: str
    state: str
    error_message: str = ""
    has_config: bool = False
    config_schema: dict[str, Any] = {}
    permissions: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}


class PluginActionResponse(BaseModel):
    plugin_id: str
    action: str
    state: str
    status: str


class PluginConfigRequest(BaseModel):
    config: dict[str, Any]


def _get_manager(request: Request) -> Any:
    """获取 PluginManager 实例"""
    mgr = getattr(request.app.state, "plugin_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Plugin manager not initialized")
    return mgr


@router.get("", response_model=Envelope[list[PluginSummary]])
async def list_plugins(request: Request) -> Any:
    """列出所有插件"""
    mgr = _get_manager(request)
    result = []
    for record in mgr.list_plugins():
        m = record.manifest
        result.append(PluginSummary(
            id=m.id,
            name=m.name,
            version=m.version,
            description=m.description,
            state=record.state.value,
            error_message=record.error_message,
            has_config=bool(m.config_schema),
            config_schema=m.config_schema,
            permissions=m.permissions.model_dump(),
            capabilities=m.capabilities.model_dump(),
        ))
    return Envelope(data=result)


@router.get("/{plugin_id}", response_model=Envelope[PluginSummary])
async def get_plugin(plugin_id: str, request: Request) -> Any:
    """获取单个插件详情"""
    mgr = _get_manager(request)
    record = mgr.get_plugin(plugin_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")
    m = record.manifest
    return Envelope(data=PluginSummary(
        id=m.id, name=m.name, version=m.version, description=m.description,
        state=record.state.value, error_message=record.error_message,
        has_config=bool(m.config_schema), config_schema=m.config_schema,
        permissions=m.permissions.model_dump(), capabilities=m.capabilities.model_dump(),
    ))


@router.post("/{plugin_id}/load", response_model=Envelope[PluginActionResponse])
async def load_plugin(plugin_id: str, request: Request) -> Any:
    mgr = _get_manager(request)
    ok = await mgr.load(plugin_id)
    record = mgr.get_plugin(plugin_id)
    return Envelope(data=PluginActionResponse(
        plugin_id=plugin_id, action="load",
        state=record.state.value if record else "unknown",
        status="ok" if ok else "failed",
    ))


@router.post("/{plugin_id}/enable", response_model=Envelope[PluginActionResponse])
async def enable_plugin(plugin_id: str, request: Request) -> Any:
    mgr = _get_manager(request)
    ok = await mgr.enable(plugin_id)
    record = mgr.get_plugin(plugin_id)
    return Envelope(data=PluginActionResponse(
        plugin_id=plugin_id, action="enable",
        state=record.state.value if record else "unknown",
        status="ok" if ok else "failed",
    ))


@router.post("/{plugin_id}/disable", response_model=Envelope[PluginActionResponse])
async def disable_plugin(plugin_id: str, request: Request) -> Any:
    mgr = _get_manager(request)
    ok = await mgr.disable(plugin_id)
    record = mgr.get_plugin(plugin_id)
    return Envelope(data=PluginActionResponse(
        plugin_id=plugin_id, action="disable",
        state=record.state.value if record else "unknown",
        status="ok" if ok else "failed",
    ))


@router.post("/{plugin_id}/reload", response_model=Envelope[PluginActionResponse])
async def reload_plugin(plugin_id: str, request: Request) -> Any:
    mgr = _get_manager(request)
    ok = await mgr.reload(plugin_id)
    record = mgr.get_plugin(plugin_id)
    return Envelope(data=PluginActionResponse(
        plugin_id=plugin_id, action="reload",
        state=record.state.value if record else "unknown",
        status="ok" if ok else "failed",
    ))


@router.post("/{plugin_id}/unload", response_model=Envelope[PluginActionResponse])
async def unload_plugin(plugin_id: str, request: Request) -> Any:
    mgr = _get_manager(request)
    ok = await mgr.unload(plugin_id)
    record = mgr.get_plugin(plugin_id)
    return Envelope(data=PluginActionResponse(
        plugin_id=plugin_id, action="unload",
        state=record.state.value if record else "unknown",
        status="ok" if ok else "failed",
    ))


@router.get("/{plugin_id}/config", response_model=Envelope[dict])
async def get_plugin_config(plugin_id: str, request: Request) -> Any:
    """获取插件配置"""
    mgr = _get_manager(request)
    return Envelope(data=mgr.get_plugin_config(plugin_id))


@router.put("/{plugin_id}/config", response_model=Envelope[dict])
async def set_plugin_config(plugin_id: str, req: PluginConfigRequest, request: Request) -> Any:
    """保存插件配置"""
    mgr = _get_manager(request)
    mgr.set_plugin_config(plugin_id, req.config)
    return Envelope(data={"status": "ok"})


@router.post("/discover", response_model=Envelope[dict])
async def discover_plugins(request: Request) -> Any:
    """触发插件扫描"""
    mgr = _get_manager(request)
    new_ids = mgr.discover()
    return Envelope(data={"discovered": new_ids})
