"""共享的环境变量读取与错误判断工具函数。

统一了 agent_dispatcher / klee_agent / tts_engine 中的重复实现。
"""
from __future__ import annotations

import os
from pathlib import Path


def read_env_key(env_var: str) -> str:
    """读取环境变量或 .env 文件中的配置值。

    优先从 os.environ 读取，不存在时从 .env 文件逐行扫描。
    """
    key = os.environ.get(env_var, "")
    if key:
        return key
    try:
        from config import ENV_PATH
        env_path = Path(ENV_PATH)
    except ImportError:
        env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith(f"{env_var}="):
                return line.split("=", 1)[1].strip()
    return ""


def is_tool_unsupported_error(error_str: str) -> bool:
    """判断错误字符串是否表示模型不支持工具调用。"""
    lower = error_str.lower()
    keywords = ["tool", "function", "not support", "unsupported", "does not have"]
    return any(kw in lower for kw in keywords)
