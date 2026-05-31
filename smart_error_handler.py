import re
import json
import asyncio
from typing import Optional, Callable
from loguru import logger


class SmartErrorHandler:

    ERROR_PATTERNS = {
        "rate_limit": {
            "patterns": [r"rate.?limit", r"429", r"too many requests", r"throttl"],
            "action": "retry",
            "max_retries": 3,
            "backoff": 2.0,
        },
        "timeout": {
            "patterns": [r"timeout", r"timed?.?out", r"deadline", r"504"],
            "action": "retry",
            "max_retries": 2,
            "backoff": 1.5,
        },
        "auth": {
            "patterns": [r"401", r"unauthorized", r"invalid.?key", r"auth"],
            "action": "fail",
            "message": "API密钥无效，请检查配置",
        },
        "quota": {
            "patterns": [r"quota", r"insufficient.?balance", r"billing"],
            "action": "fallback",
            "message": "API额度不足，切换到备用模型",
        },
        "context_length": {
            "patterns": [r"context.?length", r"token.?limit", r"max.?tokens"],
            "action": "truncate",
            "message": "上下文过长，自动裁剪",
        },
        "network": {
            "patterns": [r"connection", r"network", r"dns", r"ECONNREFUSED", r"fetch.?failed"],
            "action": "retry",
            "max_retries": 3,
            "backoff": 3.0,
        },
        "tool_error": {
            "patterns": [r"tool.?call.?fail", r"function.?not.?found", r"invalid.?tool"],
            "action": "skip",
            "message": "工具调用失败，跳过该工具",
        },
    }

    def __init__(self):
        self._error_counts: dict[str, int] = {}
        self._fallback_handlers: dict[str, Callable] = {}

    def classify(self, error: Exception | str) -> Optional[dict]:
        error_str = str(error).lower()
        for error_type, config in self.ERROR_PATTERNS.items():
            for pattern in config["patterns"]:
                if re.search(pattern, error_str, re.IGNORECASE):
                    return {"type": error_type, **config}
        return None

    async def handle(self, error: Exception | str, context: dict = None) -> dict:
        classification = self.classify(error)
        if not classification:
            return {"action": "fail", "message": f"未知错误: {error}"}

        error_type = classification["type"]
        action = classification["action"]

        self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1
        logger.warning("error_handler.classified", type=error_type, action=action, count=self._error_counts[error_type])

        if action == "retry":
            return await self._handle_retry(error, classification, context)
        elif action == "fallback":
            return await self._handle_fallback(error, classification, context)
        elif action == "truncate":
            return await self._handle_truncate(error, classification, context)
        elif action == "skip":
            return {"action": "skip", "message": classification.get("message", "跳过")}
        else:
            return {"action": "fail", "message": classification.get("message", str(error))}

    async def _handle_retry(self, error, config: dict, context: dict = None) -> dict:
        max_retries = config.get("max_retries", 3)
        backoff = config.get("backoff", 2.0)
        current_retry = (context or {}).get("retry_count", 0)

        if current_retry >= max_retries:
            return {"action": "fail", "message": f"重试{max_retries}次后仍然失败: {error}"}

        wait_time = backoff ** current_retry
        logger.info("error_handler.retry", attempt=current_retry + 1, wait=f"{wait_time:.1f}s")
        await asyncio.sleep(wait_time)

        return {
            "action": "retry",
            "retry_count": current_retry + 1,
            "message": f"正在重试({current_retry + 1}/{max_retries})...",
        }

    async def _handle_fallback(self, error, config: dict, context: dict = None) -> dict:
        fallback = self._fallback_handlers.get(config.get("type"))
        if fallback:
            try:
                result = await fallback(error, context)
                return {"action": "fallback_result", "result": result}
            except Exception as e:
                logger.error("error_handler.fallback_failed", error=str(e))

        return {"action": "fail", "message": config.get("message", str(error))}

    async def _handle_truncate(self, error, config: dict, context: dict = None) -> dict:
        messages = (context or {}).get("messages", [])
        if not messages:
            return {"action": "fail", "message": "无法裁剪：没有消息历史"}

        if len(messages) <= 3:
            return {"action": "fail", "message": "消息已最短，无法继续裁剪"}

        system = messages[0] if messages and messages[0].get("role") == "system" else None
        recent = messages[-4:] if len(messages) > 4 else messages[1:]

        truncated = []
        if system:
            truncated.append(system)
        truncated.extend(recent)

        logger.info("error_handler.truncated", original=len(messages), new=len(truncated))
        return {"action": "truncated", "messages": truncated, "message": config.get("message", "")}

    def register_fallback(self, error_type: str, handler: Callable):
        self._fallback_handlers[error_type] = handler

    def get_stats(self) -> dict:
        return dict(self._error_counts)

    def reset_stats(self):
        self._error_counts.clear()
