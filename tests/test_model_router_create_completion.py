"""_create_completion 单元测试：验证调用核心的编排顺序与参数透传。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from model_router import ModelRouter


def _make_router():
    router = MagicMock(spec=ModelRouter)
    router._select_client_for_provider = AsyncMock(return_value="CLIENT")
    router._build_route_kwargs = MagicMock(return_value={"model": "m", "stream": False})
    router._get_credential_lock = MagicMock()
    router._get_credential_lock.return_value.__aenter__ = AsyncMock(return_value=None)
    router._get_credential_lock.return_value.__aexit__ = AsyncMock(return_value=False)
    return router


@pytest.mark.asyncio
async def test_create_completion_builds_kwargs_and_creates():
    router = _make_router()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value="RESP")
    router._select_client_for_provider.return_value = client

    result = await ModelRouter._create_completion(
        router, "mimo",
        model="m", messages=[{"role": "user", "content": "hi"}],
        temperature=0.7, max_tokens=100, stream=False,
        tools=None, tool_choice=None, extra_headers=None, config={"client": "mimo"},
        timeout=30,
    )
    assert result == "RESP"
    router._build_route_kwargs.assert_called_once_with(
        "m", [{"role": "user", "content": "hi"}], 0.7, 100, False,
        None, None, None, {"client": "mimo"}, "mimo",
    )
    client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_completion_passes_stream_options():
    router = _make_router()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value="RESP")
    router._select_client_for_provider.return_value = client

    await ModelRouter._create_completion(
        router, "mimo",
        model="m", messages=[], temperature=0.7, max_tokens=100, stream=True,
        tools=None, tool_choice=None, extra_headers=None, config={}, timeout=30,
        stream_options={"include_usage": True},
    )
    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["stream_options"] == {"include_usage": True}
