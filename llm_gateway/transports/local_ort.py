from __future__ import annotations

from typing import Any, AsyncIterator, Callable

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports.base import (
    Completion,
    CompletionChunk,
    CompletionRequest,
    ProviderTransport,
    TransportError,
)


class _CancelToken:
    is_cancelled = False

    def check(self) -> None:
        return None


class LocalOrtTransport(ProviderTransport):
    def __init__(
        self,
        runtime: Any,
        model: str,
        *,
        cancel_token_factory: Callable[[], Any] = _CancelToken,
    ) -> None:
        super().__init__(
            capabilities=ProviderCapabilities(streaming=True, model_discovery=True),
            default_model=model,
        )
        self._runtime = runtime
        self._cancel_token_factory = cancel_token_factory

    def _options(self, request: CompletionRequest) -> dict[str, Any]:
        options = dict(request.extra)
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["max_tokens"] = request.max_tokens
        return options

    async def complete(self, request: CompletionRequest) -> Completion:
        chunks = [chunk async for chunk in self.stream(request)]
        return Completion(
            text="".join(chunk.text for chunk in chunks),
            model=self.default_model,
            finish_reason=chunks[-1].finish_reason if chunks else "stop",
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        try:
            async for text in self._runtime.stream(request.messages, self._options(request), self._cancel_token_factory()):
                yield CompletionChunk(text=str(text), model=self.default_model)
            yield CompletionChunk(model=self.default_model, finish_reason="stop")
        except Exception as error:
            raise TransportError("stream request failed") from error

    async def health_check(self):
        from llm_gateway.transports.base import CapabilityReport

        available = bool(self._runtime.health())
        return CapabilityReport(available, self.capabilities, models=(self.default_model,) if available else (), error=None if available else "runtime unavailable")
