"""Agnes Transport - 适配 Agnes AI API"""
import os
import asyncio
from openai import AsyncOpenAI
from loguru import logger
from transports.base import ProviderTransport, TransportResponse


class AgnesTransport(ProviderTransport):

    def __init__(self) -> None:
        # 从 os.getenv() 实时读取，避免使用 config 模块级冻结变量
        _key = os.getenv("AGNES_API_KEY", "")
        _url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
        self._client = AsyncOpenAI(api_key=_key, base_url=_url) if _key else None

    @property
    def provider_name(self) -> str:
        return "agnes"

    def is_available(self) -> bool:
        return self._client is not None

    async def chat(self, model: str, messages: list[dict],
                   temperature: float = 0.7, max_tokens: int = 1500,
                   tools: list[dict] | None = None,
                   tool_choice: str | None = None,
                   stream: bool = False,
                   timeout: int = 60,
                   thinking: dict | None = None) -> TransportResponse:
        if not self._client:
            raise RuntimeError("Agnes client not initialized")

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # Agnes 可能不支持工具调用，谨慎处理
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        # 支持 thinking 参数（Agnes Thinking 模式）
        if thinking:
            kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": True}
            }

        response = await asyncio.wait_for(
            self._client.chat.completions.create(**kwargs),
            timeout=timeout,
        )

        msg = response.choices[0].message

        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
            }

        tool_calls = None
        if hasattr(msg, "tool_calls") and msg.tool_calls:
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

        return TransportResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            reasoning_content=None,
            usage=usage,
            raw_response=response,
        )
