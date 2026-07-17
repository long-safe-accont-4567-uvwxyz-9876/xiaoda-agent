"""Agnes Transport - 适配 Agnes AI API"""
import os
import asyncio
from openai import AsyncOpenAI
from transports.base import ProviderTransport, TransportResponse


class AgnesTransport(ProviderTransport):
    """Agnes AI API 的传输适配器。"""

    def __init__(self) -> None:
        """初始化 Agnes 传输适配器。"""
        # 从 os.getenv() 实时读取，避免使用 config 模块级冻结变量
        _key = os.getenv("AGNES_API_KEY", "")
        _url = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
        self._client = AsyncOpenAI(api_key=_key, base_url=_url) if _key else None

    @property
    def provider_name(self) -> str:
        """返回 provider 名称 'agnes'。"""
        return "agnes"

    def is_available(self) -> bool:
        """返回 Agnes 客户端是否已初始化。"""
        return self._client is not None

    async def chat(self, model: str, messages: list[dict],
                   temperature: float = 0.7, max_tokens: int = 1500,
                   tools: list[dict] | None = None,
                   tool_choice: str | None = None,
                   stream: bool = False,
                   timeout: int = 60,
                   thinking: dict | None = None) -> TransportResponse:
        """调用 Agnes 对话接口，返回统一格式的 TransportResponse。"""
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

        # 关键修复：必须明确传递 enable_thinking 参数，否则 agnes 使用默认行为（可能启用推理）
        # 无论 thinking 是 True 还是 False，都要传递，确保禁用推理时生效
        kwargs["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": bool(thinking)}
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
