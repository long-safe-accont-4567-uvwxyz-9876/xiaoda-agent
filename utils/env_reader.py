"""共享的环境变量读取与错误判断工具函数。

统一了 agent_dispatcher / xiaoli_agent / tts_engine 中的重复实现。
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
    """判断错误字符串是否表示模型不支持工具调用。

    修复告警风暴：原实现只匹配任一关键词（"tool"/"function"/"not support"等），
    导致大量含 "tool" 但并非"不支持工具"的错误（如 "tool call failed"、"tool result invalid"）
    被误判为不支持，触发 tools_may_not_be_supported 告警风暴（日志中 10+ 次/小时）。

    修复策略：必须同时满足"否定语义"+"工具/函数上下文"两个条件：
    - 否定语义: "not support" / "unsupported" / "does not have" / "not available"
    - 工具上下文: "tool" / "function" / "tools"
    单独出现 "tool" 或 "function" 不视为不支持。
    """
    lower = error_str.lower()
    # 否定语义关键词（必须命中其一）
    negation_keywords = ["not support", "unsupported", "does not have", "not available",
                         "doesn't support", "don't support", "cannot support"]
    # 工具/函数上下文关键词（必须同时命中其一）
    tool_keywords = ["tool", "function", "tool_call", "function_call"]
    has_negation = any(kw in lower for kw in negation_keywords)
    has_tool_context = any(kw in lower for kw in tool_keywords)
    return has_negation and has_tool_context
