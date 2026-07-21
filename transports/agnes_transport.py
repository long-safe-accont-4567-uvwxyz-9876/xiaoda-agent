"""Agnes Transport - 适配 Agnes AI API"""
import os
import asyncio
from openai import AsyncOpenAI
from transports.base import ProviderTransport, TransportResponse

# agnes API max_tokens 上限 65536，超出返回 500 invalid_request
# 直接调用 transport.chat() 的路径（绕过 model_router._build_route_kwargs）
# 必须在此处 clamp，否则会触发 agnes 服务端 500 错误并进入 fallback 链
AGNES_MAX_TOKENS_LIMIT = 65535  # 留 1 token 余量


def _clamp_agnes_max_tokens(max_tokens: int) -> int:
    """将 max_tokens 限制在 agnes API 上限内。"""
    if max_tokens > AGNES_MAX_TOKENS_LIMIT:
        return AGNES_MAX_TOKENS_LIMIT
    return max_tokens


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
                   temperature: float = 0.7, max_tokens: int = 4096,
                   tools: list[dict] | None = None,
                   tool_choice: str | None = None,
                   stream: bool = False,
                   timeout: int = 60,
                   thinking: dict | None = None) -> TransportResponse:
        """调用 Agnes 对话接口，返回统一格式的 TransportResponse。"""
        if not self._client:
            raise RuntimeError("Agnes client not initialized")

        # 防御性 clamp：即便上层（如 agent_dispatcher/task_orchestrator）
        # 直接以 ROUTE_TABLE 默认值 131072 调用，也不会触发 agnes 500 错误
        max_tokens = _clamp_agnes_max_tokens(max_tokens)

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
        # 修复：必须显式传递 enable_thinking=False，否则 agnes-2.0-flash 在边界条件下仍返回 reasoning_content
        # thinking 可能是 {"type": "enabled"} / {"type": "disabled"} / None
        _thinking_cfg = thinking or {}
        _thinking_enabled = _thinking_cfg.get("type") == "enabled"
        kwargs["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": _thinking_enabled}
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
