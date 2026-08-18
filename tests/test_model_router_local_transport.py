from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_gateway.transports import CompletionChunk
from local_ai.integration.reranker import LocalModelUnavailableError
from model_router import ModelRouteRegistry, ModelRouter


def _sample_messages():
    return [{"role": "user", "content": "你好"}]


class FakeLocalChatService:
    def __init__(self, chunks=("local",), unavailable=False):
        self._chunks = tuple(chunks)
        self._unavailable = unavailable

    async def stream(self, messages, options, route):
        if self._unavailable:
            raise LocalModelUnavailableError("selected local chat model is unavailable")
        for chunk in self._chunks:
            yield chunk


class FakeLocalTransport:
    def __init__(self, chunks=("local",), unavailable=False):
        self._chunks = tuple(chunks)
        self._unavailable = unavailable
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self._unavailable:
            raise LocalModelUnavailableError("selected local chat model is unavailable")
        for text in self._chunks:
            yield CompletionChunk(text=text, model=request.model)
        yield CompletionChunk(model=request.model, finish_reason="stop")


def _route_table():
    def _entry(model="default", client="mimo"):
        return {
            "model": model,
            "client": client,
            "max_tokens": 512,
            "thinking": {"type": "disabled"},
            "timeout": 30,
        }

    return {
        "chat": _entry(),
        "emotion_analysis": _entry(),
        "tool_result_wrap": _entry(),
        "memory_encoding": _entry(),
    }


def _make_router(service, *, table=None, chat_provider="mimo"):
    base = table or _route_table()
    base = {task: dict(entry) for task, entry in base.items()}
    base["chat"]["client"] = chat_provider
    router = ModelRouter.__new__(ModelRouter)
    router._registry = ModelRouteRegistry(base, config_service=None)
    router.TASK_TIMEOUTS = {"chat": 30}
    router._transports = {}
    if service is not None:
        router.set_local_transport(FakeLocalTransport(service._chunks, service._unavailable))
    router._current_chat_model = None
    router._credential_locks = {}
    router.cloud_client = MagicMock()
    router._select_client_for_provider = AsyncMock(return_value=router.cloud_client)
    router._handle_route_exception = AsyncMock(return_value=False)
    router._try_fallback_chain = AsyncMock(return_value=None)
    router._chat_idle = asyncio.Event()
    router._chat_idle.set()
    router._bg_llm_semaphore = asyncio.Semaphore(1)
    router._active_bg_llm_tasks = set()
    router._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    router._request_count = 0
    router._cached_tokens_total = 0
    return router


@pytest.mark.asyncio
async def test_selected_local_model_streams_through_instance():
    service = FakeLocalChatService()
    router = _make_router(service)
    with (
        patch("model_router._set_default_provider"),
        patch(
            "web.config_service.get_config_service",
            return_value=SimpleNamespace(set=lambda *a, **k: None),
        ),
    ):
        router.set_chat_model("local-ort", "local:qwen-3b")
    assert "".join([c async for c in router.chat_stream(_sample_messages())]) == "local"


@pytest.mark.asyncio
async def test_stopped_local_model_does_not_fallback_to_cloud():
    service = FakeLocalChatService(unavailable=True)
    router = _make_router(service, chat_provider="local-ort")
    with pytest.raises(LocalModelUnavailableError):
        async for _ in router.chat_stream(_sample_messages()):
            pass
    assert router.cloud_client.call_count == 0
    assert router._select_client_for_provider.await_count == 0


@pytest.mark.asyncio
async def test_selected_local_model_delegates_protocol_to_provider_transport():
    transport = FakeLocalTransport()
    router = _make_router(None, chat_provider="local-ort")
    router.set_local_transport(transport)

    result = "".join([chunk async for chunk in router.chat_stream(_sample_messages())])

    assert result == "local"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.model == "default"
    assert request.messages == tuple(_sample_messages())
    assert request.max_tokens == 2000
    assert request.temperature == 0.7
    assert router.get_transport("local-ort") is transport


@pytest.mark.asyncio
async def test_unavailable_local_transport_does_not_enter_cloud_retry_or_fallback():
    transport = FakeLocalTransport(unavailable=True)
    router = _make_router(None, chat_provider="local-ort")
    router.set_local_transport(transport)

    with pytest.raises(LocalModelUnavailableError):
        async for _ in router.chat_stream(_sample_messages()):
            pass

    assert router._select_client_for_provider.await_count == 0
    assert router._handle_route_exception.await_count == 0
    assert router._try_fallback_chain.await_count == 0


def test_classify_error_does_not_raise_name_error():
    router = _make_router(None)

    assert ModelRouter._classify_error(RuntimeError("rate limit hit")) == "rate_limit"
    assert ModelRouter._classify_error(RuntimeError("connection refused")) == "connection_error"


@pytest.mark.asyncio
async def test_selected_local_model_routes_non_streaming():
    transport = FakeLocalTransport(chunks=("local", " done"))
    router = _make_router(None)
    router.set_local_transport(transport)
    with (
        patch("model_router._set_default_provider"),
        patch(
            "web.config_service.get_config_service",
            return_value=SimpleNamespace(set=lambda *a, **k: None),
        ),
    ):
        router.set_chat_model("local-ort", "local:qwen-3b")

    result = await router.route("emotion_analysis", _sample_messages(), stream=False)

    assert result == "local done"
    assert router._select_client_for_provider.await_count == 0
