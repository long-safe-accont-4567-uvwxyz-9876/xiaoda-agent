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
from loguru import logger


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
    """凭证加密存储（使用 credential_vault 机器绑定 AES 加密）。

    与原 base64 编码的区别：
    - base64: 任何读文件者可解码（仅防明文泄露）
    - credential_vault: 机器身份绑定 + HMAC 标签 + 加密（防跨机器复制）
    """
    from security.credential_vault import encrypt
    return encrypt(plain)


def _decode_key(encoded: str) -> str | None:
    """凭证解密读取，失败返回 None。

    解码优先级（向后兼容旧版本文件格式）：
    1. credential_vault enc:v1: 加密格式（新版本推荐）
    2. 旧版 base64 编码（自动迁移到 credential_vault）
    3. 返回 None 表示无法识别（调用方按明文兜底）
    """
    # 1. 优先尝试 credential_vault 解密（识别 enc:v1: 前缀）
    try:
        from security.credential_vault import is_encrypted, decrypt, DecryptionError
        if is_encrypted(encoded):
            try:
                return decrypt(encoded)
            except DecryptionError:
                return None
    except Exception:
        logger.debug("provider_keys.vault_import_error", exc_info=True)

    # 2. 兼容旧版 base64 编码
    import base64
    try:
        return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        logger.debug("provider_keys.base64_decode_error", exc_info=True)
        return None


def load_provider_key(provider_id: str) -> str:
    """读取 provider 凭证, 文件不存在返回空串.

    自动迁移：
    - 旧版 base64 文件首次读取后自动升级到 credential_vault 加密格式
    - 明文 key 文件首次读取后自动加密存储（后续读取走解密流程）
    """
    fp = _key_file(provider_id)
    if not fp.exists():
        return ""
    raw = fp.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    decoded = _decode_key(raw)
    if decoded is not None:
        try:
            from security.credential_vault import is_encrypted
            if not is_encrypted(raw):
                fp.write_text(_encode_key(decoded) + "\n", encoding="utf-8")
        except OSError:
            pass
        return decoded
    # 明文 key 未加密：自动加密存储，后续走解密流程
    if raw and not raw.startswith("enc:"):
        try:
            fp.write_text(_encode_key(raw) + "\n", encoding="utf-8")
            return raw
        except OSError:
            pass
    from loguru import logger
    logger.warning("provider_key.unrecognized_format provider={} raw_len={}", provider_id, len(raw))
    return ""
