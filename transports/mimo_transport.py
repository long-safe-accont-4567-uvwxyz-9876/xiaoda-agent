"""MiMo Transport - 适配小米 MiMo API"""
import os
import asyncio
from openai import AsyncOpenAI
from transports.base import ProviderTransport, TransportResponse


class MiMoTransport(ProviderTransport):
    """小米 MiMo API 的传输适配器。"""

    def __init__(self) -> None:
        """初始化 MiMo 传输适配器。"""
        # 从 os.getenv() 实时读取，避免使用 config 模块级冻结变量
        _key = os.getenv("MIMO_API_KEY", "")
        _url = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
        self._client = AsyncOpenAI(api_key=_key, base_url=_url) if _key else None

    @property
    def provider_name(self) -> str:
        """返回 provider 名称 'mimo'。"""
        return "mimo"

    def is_available(self) -> bool:
        """返回 MiMo 客户端是否已初始化。"""
        return self._client is not None

    async def chat(self, model: str, messages: list[dict],
                   temperature: float = 0.7, max_tokens: int = 1500,
                   tools: list[dict] | None = None,
                   tool_choice: str | None = None,
                   stream: bool = False,
                   timeout: int = 60,
                   thinking: dict | None = None) -> TransportResponse:
        """调用 MiMo 对话接口，返回统一格式的 TransportResponse。"""
        if not self._client:
            raise RuntimeError("MiMo client not initialized")

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        response = await asyncio.wait_for(
            self._client.chat.completions.create(**kwargs),
            timeout=timeout,
        )

        msg = response.choices[0].message

        # 提取使用量
        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "cache_hit_tokens": getattr(response.usage, "prompt_cache_hit_tokens", 0) or 0,
                "cache_miss_tokens": getattr(response.usage, "prompt_cache_miss_tokens", 0) or 0,
            }

        # 提取工具调用
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {
                    "id": str(tc.id),
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": str(tc.function.arguments) if tc.function.arguments else "{}",
                    },
                }
                for tc in msg.tool_calls
            ]

        # 提取推理内容
        reasoning = getattr(msg, "reasoning_content", None) or None

        return TransportResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            reasoning_content=reasoning,
            usage=usage,
            raw_response=response,
        )
