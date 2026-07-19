"""共享工具函数"""


def safe_int(val, default):
    """安全解析整数值，非法值回退到 default."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
