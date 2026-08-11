from __future__ import annotations

from typing import Any, AsyncIterator

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports.base import (
    CapabilityReport,
    Completion,
    CompletionChunk,
    CompletionRequest,
    ProviderTransport,
    TokenUsage,
    TransportError,
    normalize_finish_reason,
    parse_tool_calls,
    request_kwargs,
)


class OpenAICompatibleTransport(ProviderTransport):
    def __init__(
        self,
        client: Any,
        *,
        capabilities: ProviderCapabilities | None = None,
        default_model: str = "",
    ) -> None:
        super().__init__(capabilities=capabilities, default_model=default_model)
        self._client = client

    async def complete(self, request: CompletionRequest) -> Completion:
        try:
            response = await self._client.chat.completions.create(**request_kwargs(request, stream=False))
            choice = response.choices[0]
            usage = getattr(response, "usage", None)
            return Completion(
                text=str(getattr(choice.message, "content", "") or ""),
                model=str(getattr(response, "model", request.model) or request.model),
                finish_reason=normalize_finish_reason(getattr(choice, "finish_reason", None)),
                tool_calls=parse_tool_calls(getattr(choice.message, "tool_calls", None)),
                usage=TokenUsage(
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                ),
                raw=response,
            )
        except Exception as error:
            raise TransportError("completion request failed") from error

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        try:
            response = await self._client.chat.completions.create(**request_kwargs(request, stream=True))
            async for item in response:
                choice = item.choices[0]
                delta = choice.delta
                yield CompletionChunk(
                    text=str(getattr(delta, "content", "") or ""),
                    model=str(getattr(item, "model", request.model) or request.model),
                    finish_reason=normalize_finish_reason(getattr(choice, "finish_reason", None)),
                    tool_calls=parse_tool_calls(getattr(delta, "tool_calls", None)),
                    raw=item,
                )
        except Exception as error:
            raise TransportError("stream request failed") from error

    async def discover_models(self) -> tuple[str, ...]:
        try:
            response = await self._client.models.list()
            models = tuple(str(model.id) for model in response.data if getattr(model, "id", None))
            return models or await super().discover_models()
        except Exception:
            return await super().discover_models()

    async def health_check(self) -> CapabilityReport:
        try:
            response = await self._client.models.list()
            models = tuple(str(model.id) for model in response.data if getattr(model, "id", None))
            return CapabilityReport(True, self.capabilities, models=models or await super().discover_models())
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return CapabilityReport(True, self.capabilities, models=await super().discover_models())
            return CapabilityReport(False, self.capabilities, error="health check failed")
