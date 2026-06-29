"""Prompt Caching 策略 - 优化 KV 缓存命中率"""
import copy
from loguru import logger

# 缓存 TTL 选项（MiMo API 仅支持 ephemeral 类型）
CACHE_TTL_5M = "5m"


def apply_cache_control(messages: list[dict],
                        cache_ttl: str = CACHE_TTL_5M,
                        max_breakpoints: int = 4) -> list[dict]:
    """在消息上添加缓存断点标记

    策略：system_only
    - 仅在 system prompt（role="system" 的消息）上加缓存断点
    - 不在动态消息（user/assistant）上放置断点
    - 保持 ephemeral 类型

    适用于 Anthropic 兼容接口（MiMo 可能支持）。
    对于不支持的接口，此函数为 no-op。

    Args:
        messages: API 请求消息列表
        cache_ttl: 缓存 TTL（仅支持 "5m"，MiMo API 仅支持 ephemeral）
        max_breakpoints: 最大断点数

    Returns:
        添加了缓存标记的消息列表（深拷贝）
    """
    if not messages:
        return messages

    messages = copy.deepcopy(messages)
    breakpoints_used = 0

    # MiMo API 仅支持 ephemeral 类型的 cache_control
    cache_marker = {"type": "ephemeral"}

    # 仅在 system prompt 上加断点
    for msg in messages:
        if msg.get("role") == "system" and breakpoints_used < max_breakpoints:
            _apply_cache_breakpoint(msg, cache_marker)
            breakpoints_used += 1

    logger.debug("prompt_caching.applied", breakpoints=breakpoints_used, ttl=cache_ttl)
    return messages

def _apply_cache_breakpoint(message: dict, cache_marker: dict) -> None:
    """在单条消息上应用缓存断点"""
    content = message.get("content")

    if isinstance(content, str):
        # 字符串内容转为列表格式以支持 cache_control
        message["content"] = [
            {"type": "text", "text": content, "cache_control": cache_marker}
        ]
    elif isinstance(content, list):
        # 列表内容：在最后一个 text 块上加 cache_control
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = cache_marker
                break
