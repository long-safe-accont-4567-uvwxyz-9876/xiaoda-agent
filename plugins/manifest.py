"""插件清单模型 + YAML 解析"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class NetworkPermission(BaseModel):
    outbound: list[str] = Field(default_factory=list, description="允许的出站 URL glob 模式")
    inbound: bool = False


class FilesystemPermission(BaseModel):
    read: list[str] = Field(default_factory=list, description="允许读取的目录 zone")
    write: list[str] = Field(default_factory=list, description="允许写入的目录 zone")


class MemoryPermission(BaseModel):
    read: bool = False
    write: bool = False


class PluginDataPermission(BaseModel):
    read: bool = False
    write: bool = False


class SystemPermission(BaseModel):
    env_vars: list[str] = Field(default_factory=list, description="允许访问的环境变量 fnmatch 模式")
    subprocess: bool = False
    signal_handlers: bool = False


class PluginPermissions(BaseModel):
    network: NetworkPermission = Field(default_factory=NetworkPermission)
    filesystem: FilesystemPermission = Field(default_factory=FilesystemPermission)
    memory: MemoryPermission = Field(default_factory=MemoryPermission)
    plugin_data: PluginDataPermission = Field(default_factory=PluginDataPermission)
    system: SystemPermission = Field(default_factory=SystemPermission)
    llm_access: bool = False


class ToolCapability(BaseModel):
    name: str
    description: str = ""


class PluginCapabilities(BaseModel):
    tools: list[ToolCapability] = Field(default_factory=list)
    subscribes_to: list[str] = Field(default_factory=list)
    emits: list[str] = Field(default_factory=list)


class PluginDependency(BaseModel):
    id: str
    version: str = "*"


class PluginManifest(BaseModel):
    """插件清单模型"""
    id: str
    name: str
    version: str = "0.1.0"
    entrypoint: str  # "module.path:ClassName"
    description: str = ""
    xiaoda_bot_version: str = ""
    sdk_version: str = ""
    load_phase: Literal["pre-agent", "post-agent"] = "post-agent"
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    capabilities: PluginCapabilities = Field(default_factory=PluginCapabilities)
    config: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[PluginDependency] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v or not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid plugin id: {v!r}")
        return v

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError(f"Entrypoint must be 'module.path:ClassName', got {v!r}")
        return v


def parse_manifest(yaml_path: str | Path) -> PluginManifest:
    """解析 plugin.yaml 文件"""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return PluginManifest(**data)
