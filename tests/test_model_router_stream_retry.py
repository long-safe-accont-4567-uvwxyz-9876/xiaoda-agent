from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from model_router import ModelRouter


def _chunk(content: str | None = None, finish_reason: str | None = None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )],
        usage=None,
    )


class _Stream:
    def __init__(self, chunks, error: Exception | None = None) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self._raised = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            if self._error is not None and not self._raised:
                self._raised = True
                raise self._error
            raise StopAsyncIteration

    async def close(self):
        return None


def _router_with_streams(streams: list[_Stream]):
    router = ModelRouter.__new__(ModelRouter)
    router._registry = SimpleNamespace(get_task_ref=lambda task: {
        "model": "test-model",
        "client": "agnes",
        "max_tokens": 32,
    })
    router.TASK_TIMEOUTS = {"chat": 1}
    router._credential_locks = {}
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=streams)
    router._select_client_for_provider = AsyncMock(return_value=client)
    router._handle_route_exception = AsyncMock(return_value=True)
    router._try_fallback_chain = AsyncMock(return_value=None)
    return router, client


@pytest.mark.asyncio
async def test_stream_does_not_restart_after_content_was_yielded():
    router, client = _router_with_streams([
        _Stream([_chunk("第一次残片")], RuntimeError("stream disconnected")),
        _Stream([_chunk("第二次完整回复"), _chunk(finish_reason="stop")]),
    ])
    received = []

    with patch("model_router.MAX_RETRIES", 1):
        with pytest.raises(RuntimeError, match="stream disconnected"):
            async for chunk in router.chat_stream(
                [{"role": "user", "content": "你好"}],
            ):
                received.append(chunk)

    assert received == ["第一次残片"]
    assert client.chat.completions.create.await_count == 1
