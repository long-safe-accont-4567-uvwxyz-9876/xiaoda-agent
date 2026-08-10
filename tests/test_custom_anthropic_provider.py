from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from web.custom_providers import AnthropicCompatClient


class _Response:
    status_code = 200
    text = ""

    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class _AsyncClient:
    def __init__(self, response: _Response, post: AsyncMock, **kwargs) -> None:
        self._response = response
        self._post = post

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        await self._post(*args, **kwargs)
        return self._response


@pytest.mark.asyncio
async def test_anthropic_provider_preserves_all_system_messages():
    post = AsyncMock()
    response = _Response({"content": [{"type": "text", "text": "ok"}], "usage": {}})

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: _AsyncClient(response, post, **kwargs),
    ):
        client = AnthropicCompatClient("key")
        await client._create(
            model="claude-test",
            messages=[
                {"role": "system", "content": "第一条"},
                {"role": "system", "content": "第二条"},
                {"role": "user", "content": "你好"},
            ],
        )

    payload = post.await_args.kwargs["json"]
    assert payload["system"] == "第一条\n\n第二条"


@pytest.mark.asyncio
async def test_anthropic_provider_converts_openai_tools():
    post = AsyncMock()
    response = _Response({"content": [{"type": "text", "text": "ok"}], "usage": {}})
    tools = [{
        "type": "function",
        "function": {
            "name": "weather",
            "description": "查询天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: _AsyncClient(response, post, **kwargs),
    ):
        client = AnthropicCompatClient("key")
        await client._create(
            model="claude-test",
            messages=[{"role": "user", "content": "北京天气"}],
            tools=tools,
            tool_choice="required",
        )

    payload = post.await_args.kwargs["json"]
    assert payload["tools"] == [{
        "name": "weather",
        "description": "查询天气",
        "input_schema": tools[0]["function"]["parameters"],
    }]
    assert payload["tool_choice"] == {"type": "any"}


@pytest.mark.asyncio
async def test_anthropic_provider_converts_tool_use_response():
    post = AsyncMock()
    response = _Response({
        "content": [
            {"type": "text", "text": "我来查询。"},
            {
                "type": "tool_use",
                "id": "toolu_123",
                "name": "weather",
                "input": {"city": "北京"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 12, "output_tokens": 8},
    })

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: _AsyncClient(response, post, **kwargs),
    ):
        client = AnthropicCompatClient("key")
        result = await client._create(
            model="claude-test",
            messages=[{"role": "user", "content": "北京天气"}],
        )

    choice = result.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content == "我来查询。"
    assert len(choice.message.tool_calls) == 1
    tool_call = choice.message.tool_calls[0]
    assert tool_call.id == "toolu_123"
    assert tool_call.type == "function"
    assert tool_call.function.name == "weather"
    assert json.loads(tool_call.function.arguments) == {"city": "北京"}


@pytest.mark.asyncio
async def test_anthropic_provider_converts_tool_call_history_and_results():
    post = AsyncMock()
    response = _Response({"content": [{"type": "text", "text": "晴天"}], "usage": {}})
    messages = [
        {"role": "user", "content": "北京天气"},
        {
            "role": "assistant",
            "content": "我来查询。",
            "tool_calls": [{
                "id": "call_123",
                "type": "function",
                "function": {"name": "weather", "arguments": '{"city":"北京"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_123", "content": '{"weather":"晴"}'},
    ]

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: _AsyncClient(response, post, **kwargs),
    ):
        client = AnthropicCompatClient("key")
        await client._create(model="claude-test", messages=messages)

    payload = post.await_args.kwargs["json"]
    assert payload["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我来查询。"},
            {
                "type": "tool_use",
                "id": "call_123",
                "name": "weather",
                "input": {"city": "北京"},
            },
        ],
    }
    assert payload["messages"][2] == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "call_123",
            "content": '{"weather":"晴"}',
        }],
    }
