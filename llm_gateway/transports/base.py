from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Sequence

from llm_gateway.contracts import ProviderCapabilities


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    tool_choice: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    finish_reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any = None


@dataclass(frozen=True)
class CompletionChunk:
    text: str = ""
    model: str = ""
    finish_reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None


@dataclass(frozen=True)
class CapabilityReport:
    available: bool
    capabilities: ProviderCapabilities
    models: tuple[str, ...] = ()
    error: str | None = None


class TransportError(RuntimeError):
    pass


def map_http_error(error: BaseException, fallback: str) -> TransportError:
    try:
        import httpx
    except ImportError:
        return TransportError(fallback)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {401, 403}:
            return TransportError("provider authentication failed")
        if status == 404:
            return TransportError("provider endpoint or model not found")
        if status == 429:
            return TransportError("provider rate limit exceeded")
        if 400 <= status < 500:
            return TransportError(f"provider rejected request with HTTP {status}")
        return TransportError(f"provider upstream failed with HTTP {status}")
    if isinstance(error, httpx.TimeoutException):
        return TransportError("provider request timed out")
    if isinstance(error, httpx.RequestError):
        return TransportError("provider connection failed")
    return TransportError(fallback)


class ProviderTransport(ABC):
    def __init__(
        self,
        *,
        capabilities: ProviderCapabilities | None = None,
        default_model: str = "",
    ) -> None:
        self.capabilities = capabilities or ProviderCapabilities()
        self.default_model = default_model

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> Completion:
        raise NotImplementedError

    @abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        raise NotImplementedError

    async def discover_models(self) -> tuple[str, ...]:
        return (self.default_model,) if self.default_model else ()

    async def health_check(self) -> CapabilityReport:
        try:
            models = await self.discover_models()
        except TransportError as error:
            return CapabilityReport(False, self.capabilities, error=str(error))
        return CapabilityReport(True, self.capabilities, models=models)

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> ProviderTransport:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()


def request_kwargs(request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": list(request.messages),
        "stream": stream,
    }
    if request.tools:
        payload["tools"] = list(request.tools)
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    payload.update(request.extra)
    return payload


def normalize_finish_reason(reason: Any) -> str | None:
    if reason in {"end_turn", "stop_sequence", "stop", "eos"}:
        return "stop"
    if reason in {"tool_use", "tool_calls"}:
        return "tool_calls"
    if reason in {"max_tokens", "length"}:
        return "length"
    return str(reason) if reason else None


def parse_tool_calls(values: Sequence[Any] | None) -> tuple[ToolCall, ...]:
    import json

    calls = []
    for value in values or ():
        function = value.get("function", {}) if isinstance(value, Mapping) else getattr(value, "function", None)
        identifier = value.get("id", "") if isinstance(value, Mapping) else getattr(value, "id", "")
        if isinstance(function, Mapping):
            name = function.get("name", "")
            arguments = function.get("arguments", {})
        else:
            name = getattr(function, "name", "")
            arguments = getattr(function, "arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        calls.append(ToolCall(str(identifier), str(name), arguments if isinstance(arguments, Mapping) else {}))
    return tuple(calls)
