"""Provider 凭证读写工具 —— 从 web.routers.models 抽取.

原 web.routers.models.load_provider_key 被 model_router / web.routers.model_discovery
反向导入, 形成:
    model_router -> web.routers.models -> model_router
    web.routers.model_discovery -> web.routers.models -> web.routers.model_discovery

将凭证读写 (_get_cred_dir / _mask / _key_file / load_provider_key) 与
ROUTE_EDITABLE_FIELDS 常量抽到本模块, 该模块仅依赖 config, 不依赖任何 web.routers
或 model_router, 从而打破循环.
"""
from __future__ import annotations

from pathlib import Path


# 路由表可编辑字段 (供 web.routers.models / web.agent_registry 等使用)
ROUTE_EDITABLE_FIELDS = {"model", "client", "max_tokens", "thinking", "timeout"}


def _get_cred_dir() -> Path:
    """获取凭证目录 (从 config 读取, 适配 PyInstaller 与开发环境)."""
    from config import get_credentials_dir
    return get_credentials_dir()


def _mask(key: str) -> str:
    """凭证脱敏: 显示前 3 位与后 4 位, 过短则全屏蔽."""
    if not key:
        return ""
    return f"{key[:3]}***{key[-4:]}" if len(key) > 8 else "***"


def _key_file(provider_id: str) -> Path:
    """根据 provider_id 计算凭证文件路径 (过滤非法字符)."""
    safe = "".join(c for c in provider_id if c.isalnum() or c in "-_")
    return _get_cred_dir() / f"provider_{safe}.key"


def load_provider_key(provider_id: str) -> str:
    """读取 provider 凭证, 文件不存在返回空串."""
    fp = _key_file(provider_id)
    if fp.exists():
        return fp.read_text(encoding="utf-8").strip()
    return ""
