"""共享工具函数"""

# WebUI 默认监听端口。所有模块（agent.py / doctor / watchdog / cli_client /
# system 路由）统一引用此常量，避免 "8082" 魔法数字散落多处导致端口不一致。
DEFAULT_WEBUI_PORT = 8082


def safe_int(val, default):
    """安全解析整数值，非法值回退到 default."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
