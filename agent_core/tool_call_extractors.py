"""ToolCallExtractor 统一接口 —— 拆分自 agent_dispatcher.py。

内容：ExtractedToolCall 数据类 + StandardExtractor / DsmlExtractor 策略实现 +
ResourceBackend Protocol + ToolCallExtractor Protocol。函数体逐字节搬移。

外部消费者：仅 agent_dispatcher.py（SubAgent 内部使用）。agent_dispatcher
re-export 保持兼容。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls


# ── ToolCallExtractor 统一接口 ──────────────────────────────

@dataclass
class ExtractedToolCall:
    """统一的工具调用结构，无论来源是标准 tool_calls 还是 DSML 文本。"""
    id: str
    name: str
    arguments_json: str  # JSON string

    def parse_arguments(self) -> dict:
        try:
            return json.loads(self.arguments_json)
        except json.JSONDecodeError:
            return {}


@runtime_checkable
class ToolCallExtractor(Protocol):
    """从 LLM 响应中提取工具调用的策略接口。"""

    def extract(self, message: Any) -> list[ExtractedToolCall] | None:
        """从 message 中提取工具调用。返回 None 表示无工具调用。"""
        ...


class StandardExtractor:
    """从标准 message.tool_calls 中提取工具调用。"""

    def extract(self, message: Any) -> list[ExtractedToolCall] | None:
        if not message.tool_calls:
            return None
        return [
            ExtractedToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments_json=tc.function.arguments,
            )
            for tc in message.tool_calls
        ]


class DsmlExtractor:
    """从 DSML 文本标记中提取工具调用（用于推理模型）。"""

    def __init__(self, allowed_tools: set[str] | None = None) -> None:
        self._allowed_tools = allowed_tools

    def extract(self, message: Any) -> list[ExtractedToolCall] | None:
        content = message.content or ""
        if not content:
            return None
        if not has_dsml_tool_calls(content):
            return None
        dsml_calls = parse_dsml_tool_calls(content, self._allowed_tools)
        if not dsml_calls:
            return None
        return [
            ExtractedToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments_json=tc["function"]["arguments"],
            )
            for tc in dsml_calls
        ]


@runtime_checkable
class ResourceBackend(Protocol):
    def read(self, path: str) -> str:
        ...

    def write(self, path: str, content: str) -> None:
        ...

    def edit(self, path: str, old: str, new: str) -> None:
        ...

    def glob(self, pattern: str) -> list[str]:
        ...

    def grep(self, pattern: str, path: str = "/") -> list[str]:
        ...
