"""自定义模型 Provider 支持（R4/R13）。

- openai 格式：AsyncOpenAI + 自定义 base_url（复用现有依赖）
- anthropic 格式：用 httpx 直连 /v1/messages 的轻量适配器，
  对外暴露与 OpenAI SDK 相同的 client.chat.completions.create() 形状，
  这样 ModelRouter 的调用点零改动即可使用。
- custom-mapping 格式：同样暴露 OpenAI 形状的轻量适配器，
  内部复用 llm_gateway.transports.CustomMappingTransport 的
  complete()/stream()，并把 Completion/CompletionChunk 转成 OpenAI 风格对象。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping

from loguru import logger

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports import (
    AnthropicTransport,
    CompletionRequest,
    CustomMappingTransport,
)
from security.ssrf_guard import SecureAsyncTransport, build_secure_async_client, resolve_and_pin

PinningAsyncTransport = SecureAsyncTransport


class _Usage(SimpleNamespace):
    def __getattr__(self, name: Any) -> None:
        return None


def _to_openai_response(content: str, model: str, input_tokens: int, output_tokens: int,
                        tool_calls: list[Any] | None = None,
                        finish_reason: str = "stop") -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls,
                              reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
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

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type", "text") == "text"
            )
        return str(content or "")

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        converted = []
        for tool in tools:
            function = tool.get("function", {})
            converted.append({
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object"}),
            })
        return converted

    @staticmethod
    def _convert_tool_choice(tool_choice: Any) -> dict | None:
        if tool_choice in (None, "auto"):
            return None
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return {"type": "none"}
        if isinstance(tool_choice, dict):
            function = tool_choice.get("function", {})
            name = function.get("name")
            if name:
                return {"type": "tool", "name": name}
        return None

    @staticmethod
    def _convert_messages(messages: list[dict]) -> list[dict]:
        converted = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id", ""),
                        "content": str(message.get("content") or ""),
                    }],
                })
                continue
            content: Any = AnthropicCompatClient._text_content(message.get("content"))
            tool_calls = message.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tool_call in tool_calls:
                    function = tool_call.get("function", {})
                    arguments = function.get("arguments") or "{}"
                    try:
                        tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except (TypeError, json.JSONDecodeError):
                        tool_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": tool_input,
                    })
                content = blocks
            converted.append({
                "role": "assistant" if role == "assistant" else "user",
                "content": content or "",
            })
        return converted

    async def _create(self, model: str, messages: list[dict],
                      temperature: float = 0.7, max_tokens: int = 1024,
                      stream: bool = False, **kwargs: Any) -> Any:
        connect_url, host = resolve_and_pin(self._base_url)
        if stream:
            transport = AnthropicTransport(self._api_key, connect_url, host=host)
            chunks = transport.stream(CompletionRequest(
                model=model,
                messages=tuple(messages),
                tools=tuple(kwargs.get("tools") or ()),
                tool_choice=kwargs.get("tool_choice"),
                temperature=temperature,
                max_tokens=max_tokens,
            ))

            async def compatible_stream():
                try:
                    async for chunk in chunks:
                        tool_calls = [
                            SimpleNamespace(
                                id=call.id,
                                type="function",
                                function=SimpleNamespace(
                                    name=call.name,
                                    arguments=json.dumps(call.arguments, ensure_ascii=False),
                                ),
                            )
                            for call in chunk.tool_calls
                        ]
                        delta = SimpleNamespace(
                            content=chunk.text,
                            reasoning_content=None,
                            tool_calls=tool_calls or None,
                        )
                        choice = SimpleNamespace(
                            delta=delta,
                            finish_reason=chunk.finish_reason,
                        )
                        yield SimpleNamespace(choices=[choice], model=chunk.model or model)
                finally:
                    await transport.aclose()

            return compatible_stream()
        system_parts = [
            self._text_content(m.get("content"))
            for m in messages if m.get("role") == "system"
        ]
        chat_messages = self._convert_messages(messages)
        if not chat_messages:
            chat_messages = [{"role": "user", "content": ""}]
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(part for part in system_parts if part)
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = self._convert_tools(tools)
        anthropic_tool_choice = self._convert_tool_choice(kwargs.get("tool_choice"))
        if anthropic_tool_choice is not None:
            payload["tool_choice"] = anthropic_tool_choice
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if host:
            headers["Host"] = host
        async with build_secure_async_client(self._base_url, timeout=120) as client:
            resp = await client.post(f"{connect_url}/v1/messages",
                                     json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        tool_calls = [
            SimpleNamespace(
                id=block.get("id", ""),
                type="function",
                function=SimpleNamespace(
                    name=block.get("name", ""),
                    arguments=json.dumps(block.get("input", {}), ensure_ascii=False),
                ),
            )
            for block in data.get("content", [])
            if block.get("type") == "tool_use"
        ]
        usage = data.get("usage", {})
        return _to_openai_response(text, model,
                                   usage.get("input_tokens", 0),
                                   usage.get("output_tokens", 0),
                                   tool_calls or None,
                                   "tool_calls" if tool_calls else "stop")


class CustomMappingCompatClient:
    """custom_mapping 提供方适配器，形状兼容 OpenAI AsyncClient。

    对外形状与 AnthropicCompatClient 一致：
    ``client.chat.completions.create(model, messages, temperature,
    max_tokens, stream, **kwargs)`` 返回 OpenAI 风格的
    SimpleNamespace（choices[].message/delta、usage、model）。
    内部复用 CustomMappingTransport 的 complete()/stream()，
    使 ModelRouter 的调用点零改动即可使用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        mapping: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        capabilities: ProviderCapabilities | None = None,
        default_model: str = "",
        chat_path: str = "/chat/completions",
        models_path: str = "/models",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._mapping = dict(mapping or {})
        self._headers = dict(headers or {})
        self._capabilities = capabilities
        self._default_model = default_model
        self._chat_path = chat_path
        self._models_path = models_path
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _transport(self) -> CustomMappingTransport:
        return CustomMappingTransport(
            self._base_url,
            mapping=self._mapping,
            headers=self._headers,
            api_key=self._api_key,
            capabilities=self._capabilities,
            default_model=self._default_model,
            chat_path=self._chat_path,
            models_path=self._models_path,
        )

    @staticmethod
    def _convert_tool_calls(tool_calls: Any) -> list[Any] | None:
        converted = [
            SimpleNamespace(
                id=call.id,
                type="function",
                function=SimpleNamespace(
                    name=call.name,
                    arguments=json.dumps(call.arguments, ensure_ascii=False),
                ),
            )
            for call in tool_calls
        ]
        return converted or None

    async def _create(self, model: str, messages: list[dict],
                      temperature: float = 0.7, max_tokens: int = 1024,
                      stream: bool = False, **kwargs: Any) -> Any:
        transport = self._transport()
        request = CompletionRequest(
            model=model,
            messages=tuple(messages),
            tools=tuple(kwargs.get("tools") or ()),
            tool_choice=kwargs.get("tool_choice"),
            temperature=temperature,
            max_tokens=max_tokens,
            extra=dict(kwargs),
        )
        if stream:
            chunks = transport.stream(request)

            async def compatible_stream():
                try:
                    async for chunk in chunks:
                        delta = SimpleNamespace(
                            content=chunk.text,
                            reasoning_content=None,
                            tool_calls=self._convert_tool_calls(chunk.tool_calls),
                        )
                        choice = SimpleNamespace(
                            delta=delta,
                            finish_reason=chunk.finish_reason,
                        )
                        yield SimpleNamespace(choices=[choice], model=chunk.model or model)
                finally:
                    await transport.aclose()

            return compatible_stream()
        try:
            completion = await transport.complete(request)
        finally:
            await transport.aclose()
        tool_calls = self._convert_tool_calls(completion.tool_calls)
        return _to_openai_response(
            completion.text,
            completion.model or model,
            completion.usage.prompt_tokens,
            completion.usage.completion_tokens,
            tool_calls,
            completion.finish_reason or ("tool_calls" if tool_calls else "stop"),
        )


def build_client(fmt: str, base_url: str, api_key: str) -> Any:
    """按 format 构建客户端实例。"""
    if fmt == "anthropic":
        return AnthropicCompatClient(api_key=api_key, base_url=base_url)
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=build_secure_async_client(base_url),
    )


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
