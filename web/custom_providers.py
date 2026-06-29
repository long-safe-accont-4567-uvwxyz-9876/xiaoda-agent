"""自定义模型 Provider 支持（R4/R13）。

- openai 格式：AsyncOpenAI + 自定义 base_url（复用现有依赖）
- anthropic 格式：用 httpx 直连 /v1/messages 的轻量适配器，
  对外暴露与 OpenAI SDK 相同的 client.chat.completions.create() 形状，
  这样 ModelRouter 的调用点零改动即可使用。
"""
from __future__ import annotations
from typing import Any

from types import SimpleNamespace

from loguru import logger


class _Usage(SimpleNamespace):
    def __getattr__(self, name: Any) -> None:
        return None


def _to_openai_response(content: str, model: str, input_tokens: int, output_tokens: int) -> Any:
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = _Usage(prompt_tokens=input_tokens, completion_tokens=output_tokens,
                   total_tokens=input_tokens + output_tokens,
                   prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=input_tokens)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


class AnthropicCompatClient:
    """Anthropic Messages API 适配器，形状兼容 OpenAI AsyncClient。"""

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url[:-3]
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, model: str, messages: list[dict],
                      temperature: float = 0.7, max_tokens: int = 1024,
                      stream: bool = False, **kwargs: Any) -> Any:
        import httpx
        if stream:
            raise RuntimeError("Anthropic 适配器暂不支持流式")
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        chat_messages = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            content = m.get("content")
            if isinstance(content, list):  # 剥掉 cache_control 等结构
                content = "".join(
                    c.get("text", "") for c in content if isinstance(c, dict))
            chat_messages.append({"role": "assistant" if role == "assistant" else "user",
                                  "content": content or ""})
        if not chat_messages:
            chat_messages = [{"role": "user", "content": ""}]
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_parts:
            sys_text = system_parts[0]
            if isinstance(sys_text, list):
                sys_text = "".join(c.get("text", "") for c in sys_text if isinstance(c, dict))
            payload["system"] = sys_text
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self._base_url}/v1/messages",
                                     json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage", {})
        return _to_openai_response(text, model,
                                   usage.get("input_tokens", 0),
                                   usage.get("output_tokens", 0))


def build_client(fmt: str, base_url: str, api_key: str) -> Any:
    """按 format 构建客户端实例。"""
    if fmt == "anthropic":
        return AnthropicCompatClient(api_key=api_key, base_url=base_url)
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


def register_into_router(router: Any, provider_id: str, fmt: str,
                         base_url: str, api_key: str) -> None:
    """把自定义 provider 客户端注册进 ModelRouter._custom_clients。"""
    if not hasattr(router, "_custom_clients"):
        router._custom_clients = {}
    router._custom_clients[provider_id] = build_client(fmt, base_url, api_key)
    logger.info("custom_provider.registered id={} format={}", provider_id, fmt)


def unregister_from_router(router: Any, provider_id: str) -> None:
    if hasattr(router, "_custom_clients"):
        router._custom_clients.pop(provider_id, None)
