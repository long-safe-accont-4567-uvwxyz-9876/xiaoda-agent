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


def _encode_key(plain: str) -> str:
    """凭证编码存储（base64，非加密但防止明文泄露）。"""
    import base64
    return base64.b64encode(plain.encode("utf-8")).decode("ascii")


def _decode_key(encoded: str) -> str | None:
    """凭证解码读取，失败返回 None（兼容旧版明文）。"""
    import base64
    try:
        return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def load_provider_key(provider_id: str) -> str:
    """读取 provider 凭证, 文件不存在返回空串.
    
    兼容旧版明文存储：先尝试 base64 解码，失败则按明文读取并自动迁移编码。
    """
    fp = _key_file(provider_id)
    if not fp.exists():
        return ""
    raw = fp.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    # 先尝试 base64 解码（新版格式）
    decoded = _decode_key(raw)
    if decoded is not None:
        return decoded
    # 兼容旧版明文：返回原文，并自动迁移为编码存储
    try:
        fp.write_text(_encode_key(raw) + "\n", encoding="utf-8")
    except OSError:
        pass
    return raw
