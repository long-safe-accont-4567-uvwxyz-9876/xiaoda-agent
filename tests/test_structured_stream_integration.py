from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent_core.mixins.verification as verification_module
from agent_core.mixins.streaming import StreamingMixin
from agent_core.mixins.verification import VerificationMixin
from llm_gateway.router_execution import ExecutionMixin
from llm_gateway.stream_protocol import ModelStreamEvent, StreamTurnResult
from llm_gateway.transports.base import ToolCall
from tool_engine.tool_call_handler import ToolCallHandler
from tool_engine.tool_registry import ToolResult


def _tool_delta(
    *,
    index: int,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=finish_reason,
        )],
        usage=None,
    )


class _Stream:
    def __init__(self, chunks: list[SimpleNamespace], error: Exception | None = None) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self._raised = False
        self.closed = False

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

    async def close(self) -> None:
        self.closed = True


class _StructuredRouter(ExecutionMixin):
    TASK_TIMEOUTS = {"chat": 1}

    def __init__(self, streams: list[_Stream]) -> None:
        self._registry = SimpleNamespace(get_task_ref=lambda _task: {
            "model": "test-model",
            "client": "openai",
            "max_tokens": 64,
        })
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=streams)
        self.client = client
        self._select_client_for_provider = AsyncMock(return_value=client)
        self._handle_route_exception = AsyncMock(return_value=False)
        self._try_fallback_chain = AsyncMock(return_value=None)
        self._record_stream_usage = AsyncMock()

    def _apply_prompt_caching(self, _provider, messages):
        return messages

    def _apply_caching_headers(self, headers):
        return headers

    def _filter_tools_for_model(self, tools, _model):
        return tools

    @asynccontextmanager
    async def _get_provider_call_semaphore(self, _provider):
        yield


@pytest.mark.asyncio
async def test_chat_stream_events_assembles_standard_tool_call_across_chunks() -> None:
    router = _StructuredRouter([_Stream([
        _chunk(content="checking "),
        _chunk(tool_calls=[_tool_delta(
            index=0, call_id="call_", name="get_", arguments='{"city":',
        )]),
        _chunk(tool_calls=[_tool_delta(
            index=0, call_id="1", name="weather", arguments='"北京"}',
        )]),
        _chunk(finish_reason="tool_calls"),
    ])])

    events = [event async for event in router.chat_stream_events(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        turn=3,
    )]

    assert [event.kind for event in events] == [
        "text_delta", "tool_call_delta", "tool_call_delta", "turn_end",
    ]
    assert all(event.turn == 3 for event in events)
    assert events[-1].finish_reason == "tool_calls"
    assert events[-1].tool_calls[0].id == "call_1"
    assert events[-1].tool_calls[0].name == "get_weather"
    assert events[-1].tool_calls[0].arguments == {"city": "北京"}
    assert events[-1].provider == "openai"
    assert events[-1].model == "test-model"
    assert events[-1].fallback is False


@pytest.mark.asyncio
async def test_legacy_chat_stream_still_yields_only_text() -> None:
    router = _StructuredRouter([_Stream([
        _chunk(content="visible"),
        _chunk(tool_calls=[_tool_delta(
            index=0, call_id="call_1", name="weather", arguments="{}",
        )]),
        _chunk(finish_reason="tool_calls"),
    ])])

    assert [part async for part in router.chat_stream(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )] == ["visible"]


@pytest.mark.asyncio
async def test_structured_stream_closes_upstream_when_consumer_cancels() -> None:
    stream = _Stream([
        _chunk(content="first"),
        _chunk(content="second"),
        _chunk(finish_reason="stop"),
    ])
    router = _StructuredRouter([stream])

    generator = router.chat_stream_events(
        [{"role": "user", "content": "hello"}],
    )
    first = await generator.__anext__()
    assert first.text_delta == "first"

    await generator.aclose()

    assert stream.closed is True


@pytest.mark.asyncio
async def test_structured_stream_does_not_replay_after_visible_text_failure() -> None:
    router = _StructuredRouter([
        _Stream([_chunk(content="partial")], RuntimeError("disconnected")),
        _Stream([_chunk(content="duplicate"), _chunk(finish_reason="stop")]),
    ])
    received: list[ModelStreamEvent] = []

    with pytest.raises(RuntimeError, match="disconnected"):
        async for event in router.chat_stream_events(
            [{"role": "user", "content": "hello"}],
        ):
            received.append(event)

    assert [event.text_delta for event in received] == ["partial"]
    assert router.client.chat.completions.create.await_count == 1


class _StreamingHarness(StreamingMixin):
    def __init__(self, events: list[ModelStreamEvent]) -> None:
        self.router = SimpleNamespace(chat_stream_events=self._events)
        self.events = events

    async def _events(self, *_args, **_kwargs):
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_stream_llm_turn_hides_dsml_preview_but_keeps_raw_for_parser() -> None:
    raw = (
        'before<tool_call>{"name":"weather","arguments":{"city":"北京"}}'
        '</tool_call>after'
    )
    harness = _StreamingHarness([
        ModelStreamEvent(kind="text_delta", turn=0, text_delta=raw[:20]),
        ModelStreamEvent(kind="text_delta", turn=0, text_delta=raw[20:]),
        ModelStreamEvent(kind="turn_end", turn=0, finish_reason="tool_calls"),
    ])
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    result = await harness._stream_llm_turn(
        [], status_callback=collect_status, tools=[{"type": "function"}],
    )

    assert result.text == raw
    assert "<tool_call" not in "".join(status["delta"] for status in statuses)
    assert "before" in statuses[0]["delta"]


class _VerificationHarness(VerificationMixin):
    LLM_CALL_TIMEOUT = 5
    VERIFICATION_WALL_TIMEOUT = 20

    def __init__(self, turns: list[StreamTurnResult]) -> None:
        self.turns = iter(turns)
        self.seen_turns: list[int] = []
        self.tool_repair = SimpleNamespace(_allowed_tools={"first", "second"})

    async def _stream_llm_turn(self, *_args, turn: int, **_kwargs):
        self.seen_turns.append(turn)
        return next(self.turns)

    @staticmethod
    def _clean_reply(text: str) -> str:
        return text


@pytest.mark.asyncio
async def test_verification_streams_second_tool_turn_and_final_text(monkeypatch) -> None:
    monkeypatch.setattr(verification_module, "STRUCTURED_STREAM_EVENTS", True)
    harness = _VerificationHarness([
        StreamTurnResult(
            text="", tool_calls=(ToolCall("call-2", "second", {"value": 2}),),
            finish_reason="tool_calls", reasoning="", provider="openai",
            model="test", used_fallback=False, turn=1,
        ),
        StreamTurnResult(
            text="final answer.", tool_calls=(), finish_reason="stop", reasoning="",
            provider="openai", model="test", used_fallback=False, turn=2,
        ),
    ])
    trace = MagicMock()

    second = await harness._call_and_parse_verification_llm(
        [], [{"type": "function"}], "chat", 0.7, 64,
        "u", "s", trace, 0, __import__("time").time(), status_callback=AsyncMock(),
    )
    final = await harness._call_and_parse_verification_llm(
        [], [{"type": "function"}], "chat", 0.7, 64,
        "u", "s", trace, 1, __import__("time").time(), status_callback=AsyncMock(),
    )

    assert second[0][0]["id"] == "call-2"
    assert second[0][0]["function"]["arguments"] == '{"value": 2}'
    assert final[3] == "final answer."
    assert harness.seen_turns == [1, 2]


def test_legacy_dsml_same_tool_uses_explicit_turn_identity() -> None:
    harness = _VerificationHarness([])
    harness.router = SimpleNamespace(pop_reasoning_content=lambda: None)
    raw = '<tool_call>{"name":"first","arguments":{"value":1}}</tool_call>'

    first, *_ = harness._parse_verification_result(
        raw, [{"type": "function"}], stream_turn=1,
    )
    second, *_ = harness._parse_verification_result(
        raw, [{"type": "function"}], stream_turn=2,
    )

    assert first[0]["id"] == "dsml:1:0:first"
    assert second[0]["id"] == "dsml:2:0:first"
    assert first[0]["_stream_turn"] == 1
    assert second[0]["_stream_turn"] == 2


@pytest.mark.asyncio
async def test_dsml_same_tool_across_turns_has_distinct_status_identity() -> None:
    harness = _VerificationHarness([])
    raw = '<tool_call>{"name":"first","arguments":{"value":1}}</tool_call>'
    calls = []
    for turn in (1, 2):
        result = StreamTurnResult(
            text=raw, tool_calls=(), finish_reason="tool_calls", reasoning="",
            provider="openai", model="test", used_fallback=False, turn=turn,
        )
        parsed, _content, _reasoning = harness._parse_verification_result(
            result, [{"type": "function"}],
        )
        calls.append(parsed[0])

    statuses: list[dict] = []
    handler = ToolCallHandler(
        tool_executor=SimpleNamespace(execute=AsyncMock(return_value=ToolResult.ok("ok"))),
        tool_repair=SimpleNamespace(
            detect_storm=lambda *_args: False,
            repair_truncation=lambda _args: None,
        ),
        clean_reply_callback=lambda text: text,
        status_callback=AsyncMock(side_effect=statuses.append),
    )
    trace = MagicMock()
    await handler._execute_single_tool(calls[0], trace)
    await handler._execute_single_tool(calls[1], trace)

    started = [status for status in statuses if status["stage"] == "started"]
    assert [call["id"] for call in calls] == [
        "dsml:1:0:first", "dsml:2:0:first",
    ]
    assert [(status["tool_call_id"], status["turn"], status["index"])
            for status in started] == [
        ("dsml:1:0:first", 1, 0),
        ("dsml:2:0:first", 2, 0),
    ]
