"""上下文使用监控 — 借鉴 Claude Agent SDK 的 ContextUsageResponse 设计"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextUsageCategory:
    """单个上下文使用类别"""
    name: str
    tokens: int
    color: str = "#888888"
    is_deferred: bool = False


@dataclass
class ContextUsageResponse:
    """上下文窗口使用分析响应"""
    categories: list[ContextUsageCategory] = field(default_factory=list)
    total_tokens: int = 0
    max_tokens: int = 0
    percentage: float = 0.0
    model: str = ""
    is_auto_compact_enabled: bool = False
    auto_compact_threshold: int = 0
    memory_files: list[dict[str, Any]] = field(default_factory=list)
    mcp_tools: list[dict[str, Any]] = field(default_factory=list)


def estimate_token_count(text: str) -> int:
    """估算文本的 token 数量

    简单估算：中文约 1.5 token/字符，英文约 0.25 token/word
    更精确的估算需要 tiktoken，但这里用轻量级方法
    """
    if not text:
        return 0

    # 区分中文和非中文字符
    chinese_chars = 0
    other_chars = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x3000 <= cp <= 0x303F:
            chinese_chars += 1
        else:
            other_chars += 1

    # 中文约 1.5 token/字符，英文/符号约 0.25 token/字符（粗略）
    return int(chinese_chars * 1.5 + other_chars * 0.25)


def compute_context_usage(
    system_prompt: str = "",
    tools_json: str = "",
    messages: list[dict] | None = None,
    model: str = "",
    max_tokens: int = 128000,
    auto_compact_threshold_ratio: float = 0.8,
) -> ContextUsageResponse:
    """计算当前上下文窗口使用情况

    Args:
        system_prompt: 系统提示词文本
        tools_json: 工具定义的 JSON 字符串
        messages: 对话历史消息列表
        model: 当前使用的模型名称
        max_tokens: 模型最大上下文窗口
        auto_compact_threshold_ratio: 自动压缩触发比例
    """
    messages = messages or []

    # 计算各部分 token
    system_tokens = estimate_token_count(system_prompt)
    tools_tokens = estimate_token_count(tools_json)

    message_tokens = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            message_tokens += estimate_token_count(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        message_tokens += estimate_token_count(text)

    total = system_tokens + tools_tokens + message_tokens
    threshold = int(max_tokens * auto_compact_threshold_ratio)
    percentage = (total / max_tokens * 100) if max_tokens > 0 else 0.0

    categories = [
        ContextUsageCategory(name="系统提示词", tokens=system_tokens, color="#4A90D9"),
        ContextUsageCategory(name="工具定义", tokens=tools_tokens, color="#7B68EE"),
        ContextUsageCategory(name="对话历史", tokens=message_tokens, color="#50C878"),
    ]

    return ContextUsageResponse(
        categories=categories,
        total_tokens=total,
        max_tokens=max_tokens,
        percentage=round(percentage, 1),
        model=model,
        is_auto_compact_enabled=True,
        auto_compact_threshold=threshold,
    )
