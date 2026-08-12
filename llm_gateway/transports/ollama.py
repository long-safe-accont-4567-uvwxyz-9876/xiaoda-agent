from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports.base import (
    CapabilityReport,
    Completion,
    CompletionChunk,
    CompletionRequest,
    ProviderTransport,
    map_http_error,
    normalize_finish_reason,
    parse_tool_calls,
)


class OllamaTransport(ProviderTransport):
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        http_client: Any = None,
        capabilities: ProviderCapabilities | None = None,
        default_model: str = "",
    ) -> None:
        super().__init__(capabilities=capabilities, default_model=default_model)
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(timeout=120)

    @staticmethod
    def _payload(request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        options = dict(request.extra)
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        payload: dict[str, Any] = {"model": request.model, "messages": list(request.messages), "stream": stream}
        if request.tools:
            payload["tools"] = list(request.tools)
        if options:
            payload["options"] = options
        return payload

    async def complete(self, request: CompletionRequest) -> Completion:
        try:
            response = await self._http.post(f"{self._base_url}/api/chat", json=self._payload(request, stream=False), headers=self._headers)
            response.raise_for_status()
            data = response.json()
            calls = parse_tool_calls(data.get("message", {}).get("tool_calls"))
            return Completion(
                text=str(data.get("message", {}).get("content", "")),
                model=str(data.get("model") or request.model),
                finish_reason=normalize_finish_reason(data.get("done_reason") or ("tool_calls" if calls else "stop" if data.get("done") else None)),
                tool_calls=calls,
                raw=data,
            )
        except Exception as error:
            raise map_http_error(error, "completion request failed") from error

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        try:
            async with self._http.stream("POST", f"{self._base_url}/api/chat", json=self._payload(request, stream=True), headers=self._headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    calls = parse_tool_calls(data.get("message", {}).get("tool_calls"))
                    yield CompletionChunk(
                        text=str(data.get("message", {}).get("content", "")),
                        model=str(data.get("model") or request.model),
                        finish_reason=normalize_finish_reason(data.get("done_reason") or ("tool_calls" if calls else "stop" if data.get("done") else None)),
                        tool_calls=calls,
                        raw=data,
                    )
        except Exception as error:
            raise map_http_error(error, "stream request failed") from error

    async def discover_models(self) -> tuple[str, ...]:
        try:
            response = await self._http.get(f"{self._base_url}/api/tags", headers=self._headers)
            response.raise_for_status()
            models = tuple(str(item["name"]) for item in response.json().get("models", ()) if item.get("name"))
            return models or await super().discover_models()
        except Exception:
            return await super().discover_models()

    async def health_check(self) -> CapabilityReport:
        try:
            response = await self._http.get(f"{self._base_url}/api/tags", headers=self._headers)
            if response.status_code == 404:
                return CapabilityReport(True, self.capabilities, models=await super().discover_models())
            response.raise_for_status()
            models = tuple(str(item["name"]) for item in response.json().get("models", ()) if item.get("name"))
            return CapabilityReport(True, self.capabilities, models=models or await super().discover_models())
        except Exception:
            return CapabilityReport(False, self.capabilities, error="health check failed")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
