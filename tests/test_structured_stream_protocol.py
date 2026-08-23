from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from llm_gateway.stream_protocol import (
    DSMLPreviewFilter,
    ModelStreamEvent,
    StreamEventSequencer,
    StreamTurnResult,
    StructuredStreamProtocolError,
    ToolCallAccumulator,
    ToolCallDelta,
    normalize_finish_reason,
)
from llm_gateway.transports.base import ToolCall


def test_tool_call_accumulator_assembles_cross_chunk_arguments() -> None:
    delta = ToolCallDelta(index=0, id_delta="call_", name_delta="get_", arguments_delta='{"city":')
    with pytest.raises(FrozenInstanceError):
        delta.index = 1  # type: ignore[misc]

    accumulator = ToolCallAccumulator()
    accumulator.add(delta)
    accumulator.add(ToolCallDelta(index=0, id_delta="1", name_delta="weather", arguments_delta='"北京"}'))

    assert accumulator.finalize() == (
        ToolCall(id="call_1", name="get_weather", arguments={"city": "北京"}),
    )


def test_tool_call_accumulator_keeps_concurrent_indexes_separate() -> None:
    accumulator = ToolCallAccumulator()
    accumulator.add(ToolCallDelta(index=1, id_delta="call_2", name_delta="search", arguments_delta='{"q":'))
    accumulator.add(ToolCallDelta(index=0, id_delta="call_1", name_delta="weather", arguments_delta='{"city":'))
    accumulator.add(ToolCallDelta(index=1, arguments_delta='"docs"}'))
    accumulator.add(ToolCallDelta(index=0, arguments_delta='"上海"}'))

    assert accumulator.finalize() == (
        ToolCall(id="call_1", name="weather", arguments={"city": "上海"}),
        ToolCall(id="call_2", name="search", arguments={"q": "docs"}),
    )


@pytest.mark.parametrize(
    ("deltas", "code"),
    [
        ([ToolCallDelta(index=3, arguments_delta="{}")], "tool_call_missing_name"),
        ([ToolCallDelta(index=4, name_delta="broken", arguments_delta="{")], "tool_call_invalid_json"),
        ([ToolCallDelta(index=5, name_delta="array", arguments_delta="[]")], "tool_call_arguments_not_object"),
    ],
)
def test_tool_call_accumulator_rejects_invalid_calls(
    deltas: list[ToolCallDelta],
    code: str,
) -> None:
    accumulator = ToolCallAccumulator()
    for delta in deltas:
        accumulator.add(delta)

    with pytest.raises(StructuredStreamProtocolError) as caught:
        accumulator.finalize()

    assert caught.value.code == code
    assert caught.value.index == deltas[0].index


@pytest.mark.parametrize(
    ("provider_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "length"),
        ("failed", "error"),
        ("canceled", "cancelled"),
        (None, None),
    ],
)
def test_finish_reason_normalization(provider_reason: object, expected: str | None) -> None:
    assert normalize_finish_reason(provider_reason) == expected


def test_protocol_result_types_are_frozen() -> None:
    event = ModelStreamEvent(kind="text_delta", turn=2, text_delta="hi", provider="openai", model="gpt")
    result = StreamTurnResult(
        text="hi",
        tool_calls=(),
        finish_reason="stop",
        reasoning="",
        provider="openai",
        model="gpt",
        used_fallback=False,
    )

    with pytest.raises(FrozenInstanceError):
        event.turn = 3  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.text = "changed"  # type: ignore[misc]

    assert event.fallback is False
    assert result.finish_reason == "stop"


@pytest.mark.parametrize(
    "chunks",
    [
        ["prefix <｜", "DSML｜function_calls><｜DSML｜invoke name=\"recall\">payload</function_calls> suffix"],
        ["prefix <｜｜DS", "ML｜｜tool_calls><｜｜DSML｜｜invoke name=\"recall\">payload</｜｜DSML｜｜tool_calls> suffix"],
        ["prefix <func", "tion_calls><invoke name=\"recall\"><parameter name=\"q\">x</parameter></invoke></function_calls> suffix"],
        ["prefix <opera", "tion>recall</operation><function_calls><param name=\"q\">x</param></function_calls> suffix"],
        ["prefix <tool_", "call>{\"name\":\"recall\",\"arguments\":{}}</tool_call> suffix"],
        ["prefix [TOOL_", "CALL]{\"tool\":\"recall\"}[/TOOL_CALL] suffix"],
        ["prefix ```tool_", "call\n{\"name\":\"recall\"}\n``` suffix"],
        ["prefix <function=", "recall><parameter=q>x</parameter></function> suffix"],
        ["prefix <read_", "file path=\"secret\">payload</read_file> suffix"],
    ],
)
def test_dsml_preview_filter_hides_cross_chunk_tool_variants(chunks: list[str]) -> None:
    preview_filter = DSMLPreviewFilter(max_buffer_chars=512)

    visible = "".join(preview_filter.feed(chunk) for chunk in chunks)
    raw = preview_filter.finish()

    assert visible == "prefix  suffix"
    assert raw == "".join(chunks)


def test_dsml_preview_filter_streams_normal_text_around_tool_block() -> None:
    preview_filter = DSMLPreviewFilter()

    assert preview_filter.feed("ordinary text") == "ordinary text"
    assert preview_filter.feed("<func") == ""
    assert preview_filter.feed("tion_calls>hidden</function_calls>") == ""
    assert preview_filter.feed(" after") == " after"
    assert preview_filter.finish() == "ordinary text<function_calls>hidden</function_calls> after"


def test_dsml_preview_filter_suppresses_unclosed_tool_block_at_end() -> None:
    preview_filter = DSMLPreviewFilter()

    assert preview_filter.feed("safe<｜DS") == "safe"
    assert preview_filter.feed("ML｜invoke name=\"recall\">secret") == ""
    assert preview_filter.finish() == 'safe<｜DSML｜invoke name="recall">secret'


def test_dsml_preview_filter_fails_closed_when_buffer_limit_is_exceeded() -> None:
    preview_filter = DSMLPreviewFilter(max_buffer_chars=32)

    assert preview_filter.feed("safe<tool_call>") == "safe"
    with pytest.raises(StructuredStreamProtocolError) as caught:
        preview_filter.feed("x" * 32)

    assert caught.value.code == "dsml_preview_buffer_exceeded"
    assert preview_filter.feed("must stay hidden</tool_call>after") == ""
    with pytest.raises(StructuredStreamProtocolError):
        preview_filter.finish()


def test_stream_event_sequencer_assigns_identity_order_and_turn() -> None:
    sequencer = StreamEventSequencer("message-1")

    first = sequencer.emit("text_delta", turn=0, delta="hello")
    second = sequencer.emit(
        "tool_status",
        turn=1,
        tool_call_id="call-1",
        index=0,
        stage="started",
    )

    assert first == {
        "type": "stream_event",
        "version": 1,
        "msg_id": "message-1",
        "seq": 1,
        "turn": 0,
        "event": "text_delta",
        "delta": "hello",
    }
    assert second["seq"] == 2
    assert second["turn"] == 1
    assert second["tool_call_id"] == "call-1"


def test_stream_event_sequencer_rejects_any_event_after_terminal() -> None:
    sequencer = StreamEventSequencer("message-2")

    terminal = sequencer.emit("final", turn=2, terminal=True, reply="done")

    assert terminal["seq"] == 1
    assert terminal["terminal"] is True
    assert sequencer.terminal is True
    with pytest.raises(StructuredStreamProtocolError) as caught:
        sequencer.emit("error", turn=2, terminal=True, code="late")
    assert caught.value.code == "stream_terminal_already_emitted"


def test_stream_event_sequencer_requires_nonnegative_turn() -> None:
    sequencer = StreamEventSequencer("message-3")

    with pytest.raises(StructuredStreamProtocolError) as caught:
        sequencer.emit("text_delta", turn=-1, delta="bad")

    assert caught.value.code == "stream_invalid_turn"
