from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    router._local_chat_service = service
    router._current_chat_model = None
    router._credential_locks = {}
    router.cloud_client = MagicMock()
    router._select_client_for_provider = AsyncMock(return_value=router.cloud_client)
    router._handle_route_exception = AsyncMock(return_value=False)
    router._try_fallback_chain = AsyncMock(return_value=None)
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