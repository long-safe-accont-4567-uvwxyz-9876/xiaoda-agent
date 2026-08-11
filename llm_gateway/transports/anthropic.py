from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping

import httpx

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports.base import (
    CapabilityReport,
    Completion,
    CompletionChunk,
    CompletionRequest,
    ProviderTransport,
    TokenUsage,
    ToolCall,
    TransportError,
    normalize_finish_reason,
)


class AnthropicTransport(ProviderTransport):
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        *,
        http_client: Any = None,
        capabilities: ProviderCapabilities | None = None,
        default_model: str = "",
    ) -> None:
        super().__init__(capabilities=capabilities, default_model=default_model)
        normalized = base_url.rstrip("/")
        self._base_url = normalized if normalized.endswith("/v1") else f"{normalized}/v1"
        self._headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(timeout=120)

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, list):
            return "".join(str(part.get("text", "")) for part in content if isinstance(part, Mapping) and part.get("type", "text") == "text")
        return str(content or "")

    @classmethod
    def _payload(cls, request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        systems = [cls._text(message.get("content")) for message in request.messages if message.get("role") == "system"]
        messages = []
        for message in request.messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "tool":
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""), "content": cls._text(message.get("content"))}]})
                continue
            content: Any = cls._text(message.get("content"))
            if role == "assistant" and message.get("tool_calls"):
                blocks = ([{"type": "text", "text": content}] if content else [])
                for call in message["tool_calls"]:
                    function = call.get("function", {})
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    blocks.append({"type": "tool_use", "id": call.get("id", ""), "name": function.get("name", ""), "input": arguments})
                content = blocks
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
        payload: dict[str, Any] = {"model": request.model, "messages": messages or [{"role": "user", "content": ""}], "max_tokens": request.max_tokens or 1024, "stream": stream}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if systems:
            payload["system"] = "\n\n".join(part for part in systems if part)
        if request.tools:
            payload["tools"] = [{"name": tool.get("function", {}).get("name", ""), "description": tool.get("function", {}).get("description", ""), "input_schema": tool.get("function", {}).get("parameters", {"type": "object"})} for tool in request.tools]
        choice = request.tool_choice
        if choice == "required":
            payload["tool_choice"] = {"type": "any"}
        elif choice == "none":
            payload["tool_choice"] = {"type": "none"}
        elif isinstance(choice, Mapping) and choice.get("function", {}).get("name"):
            payload["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        payload.update(request.extra)
        return payload

    async def complete(self, request: CompletionRequest) -> Completion:
        try:
            response = await self._http.post(f"{self._base_url}/messages", json=self._payload(request, stream=False), headers=self._headers)
            response.raise_for_status()
            data = response.json()
            calls = tuple(ToolCall(str(block.get("id", "")), str(block.get("name", "")), block.get("input", {})) for block in data.get("content", ()) if block.get("type") == "tool_use")
            usage = data.get("usage", {})
            prompt = int(usage.get("input_tokens", 0) or 0)
            completion = int(usage.get("output_tokens", 0) or 0)
            return Completion(
                text="".join(str(block.get("text", "")) for block in data.get("content", ()) if block.get("type") == "text"),
                model=str(data.get("model") or request.model),
                finish_reason=normalize_finish_reason(data.get("stop_reason")),
                tool_calls=calls,
                usage=TokenUsage(prompt, completion, prompt + completion),
                raw=data,
            )
        except Exception as error:
            raise TransportError("completion request failed") from error

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        tool_blocks: dict[int, dict[str, str]] = {}
        try:
            async with self._http.stream("POST", f"{self._base_url}/messages", json=self._payload(request, stream=True), headers=self._headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:].strip())
                    event_type = data.get("type")
                    delta = data.get("delta", {})
                    index = int(data.get("index", 0) or 0)
                    if event_type == "content_block_start" and data.get("content_block", {}).get("type") == "tool_use":
                        block = data["content_block"]
                        tool_blocks[index] = {"id": str(block.get("id", "")), "name": str(block.get("name", "")), "arguments": json.dumps(block.get("input", {})) if block.get("input") else ""}
                    elif event_type == "content_block_delta" and delta.get("type") == "input_json_delta":
                        tool_blocks.setdefault(index, {"id": "", "name": "", "arguments": ""})["arguments"] += str(delta.get("partial_json", ""))
                    elif event_type == "content_block_stop" and index in tool_blocks:
                        block = tool_blocks.pop(index)
                        try:
                            arguments = json.loads(block["arguments"] or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                        yield CompletionChunk(model=request.model, tool_calls=(ToolCall(block["id"], block["name"], arguments),), raw=data)
                    elif event_type == "content_block_delta" and delta.get("type") == "text_delta":
                        yield CompletionChunk(text=str(data["delta"].get("text", "")), model=request.model, raw=data)
                    elif event_type == "message_delta":
                        yield CompletionChunk(model=request.model, finish_reason=normalize_finish_reason(data.get("delta", {}).get("stop_reason")), raw=data)
        except Exception as error:
            raise TransportError("stream request failed") from error

    async def discover_models(self) -> tuple[str, ...]:
        try:
            response = await self._http.get(f"{self._base_url}/models", headers=self._headers)
            response.raise_for_status()
            models = tuple(str(item["id"]) for item in response.json().get("data", ()) if item.get("id"))
            return models or await super().discover_models()
        except Exception:
            return await super().discover_models()

    async def health_check(self) -> CapabilityReport:
        try:
            response = await self._http.get(f"{self._base_url}/models", headers=self._headers)
            if response.status_code == 404:
                return CapabilityReport(True, self.capabilities, models=await super().discover_models())
            response.raise_for_status()
            models = tuple(str(item["id"]) for item in response.json().get("data", ()) if item.get("id"))
            return CapabilityReport(True, self.capabilities, models=models or await super().discover_models())
        except Exception:
            return CapabilityReport(False, self.capabilities, error="health check failed")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
