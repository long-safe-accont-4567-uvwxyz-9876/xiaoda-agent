"""Transport 基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class TransportResponse:
    """统一的 Transport 响应"""
    content: str = ""
    tool_calls: list[dict] | None = None
    reasoning_content: str | None = None
    usage: dict | None = None  # {"prompt_tokens": int, "completion_tokens": int, ...}
    raw_response: Any = None   # 原始响应对象


class ProviderTransport(ABC):
    """提供商 Transport 基类"""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """提供商名称"""
        ...

    @abstractmethod
    async def chat(self, model: str, messages: list[dict],
                   temperature: float = 0.7, max_tokens: int = 4096,
                   tools: list[dict] | None = None,
                   tool_choice: str | None = None,
                   stream: bool = False,
                   timeout: int = 60,
                   thinking: dict | None = None) -> TransportResponse:
        """聊天完成接口"""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查 Transport 是否可用（API Key 是否配置等）"""
        ...
