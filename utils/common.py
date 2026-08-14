"""共享工具函数"""

import hashlib

# WebUI 默认监听端口。所有模块（agent.py / doctor / watchdog / cli_client /
# system 路由）统一引用此常量，避免 "8082" 魔法数字散落多处导致端口不一致。
DEFAULT_WEBUI_PORT = 8082

# LLM 响应 max_tokens 的默认值（生成 token 上限）。transport 层与 model_router /
# message_processor 统一引用，避免 "4096" 魔法数字散落多处。
DEFAULT_MAX_TOKENS = 4096


def safe_int(val, default):
    """安全解析整数值，非法值回退到 default."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def safe_float(val, default=0.0):
    """安全解析浮点数，None / 非法值回退到 default."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def mask_api_key(key: str) -> str:
    """返回 API key 的 sha256 前 8 位，用于日志标识而不泄漏真实 key 片段。

    同一 key 哈希稳定、不同 key 哈希不同、无法逆推原始 key。
    """
    if not key or len(key) < 8:
        return "***"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
