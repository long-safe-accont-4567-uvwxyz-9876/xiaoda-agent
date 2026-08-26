from __future__ import annotations

import json
import re
from dataclasses import dataclass

from llm_gateway.transports.base import ToolCall


class StructuredStreamProtocolError(ValueError):
    """Machine-readable error raised for an invalid structured stream."""

    def __init__(self, code: str, message: str, *, index: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.index = index


class StreamEventSequencer:
    """Assign monotonic sequence numbers and enforce a single terminal event."""

    def __init__(self, msg_id: str) -> None:
        if not msg_id:
            raise ValueError("msg_id must not be empty")
        self.msg_id = msg_id
        self._seq = 0
        self._terminal = False

    @property
    def terminal(self) -> bool:
        return self._terminal

    def emit(
        self,
        event: str,
        *,
        turn: int,
        terminal: bool = False,
        **payload: object,
    ) -> dict[str, object]:
        if self._terminal:
            raise StructuredStreamProtocolError(
                "stream_terminal_already_emitted",
                f"stream for {self.msg_id} already emitted a terminal event",
            )
        if turn < 0:
            raise StructuredStreamProtocolError(
                "stream_invalid_turn",
                "stream event turn must be nonnegative",
            )
        self._seq += 1
        envelope: dict[str, object] = {
            "type": "stream_event",
            "version": 1,
            "msg_id": self.msg_id,
            "seq": self._seq,
            "turn": turn,
            "event": event,
        }
        if terminal:
            envelope["terminal"] = True
            self._terminal = True
        envelope.update(payload)
        return envelope


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id_delta: str = ""
    name_delta: str = ""
    arguments_delta: str = ""


@dataclass(frozen=True)
class ModelStreamEvent:
    kind: str
    turn: int
    text_delta: str = ""
    reasoning_delta: str = ""
    tool_call_delta: ToolCallDelta | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    provider: str = ""
    model: str = ""
    fallback: bool = False


@dataclass(frozen=True)
class StreamTurnResult:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    reasoning: str
    provider: str
    model: str
    used_fallback: bool
    turn: int = 0


_FINISH_REASON_ALIASES = {
    "stop": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "eos": "stop",
    "completed": "stop",
    "complete": "stop",
    "tool_calls": "tool_calls",
    "tool_call": "tool_calls",
    "tool_use": "tool_calls",
    "function_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "length",
    "max_tokens": "length",
    "max_output_tokens": "length",
    "token_limit": "length",
    "error": "error",
    "failed": "error",
    "failure": "error",
    "content_filter": "error",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "cancel": "cancelled",
    "cancellation": "cancelled",
}


def normalize_finish_reason(reason: object) -> str | None:
    if reason is None:
        return None
    normalized = str(reason).strip().lower()
    if not normalized:
        return None
    return _FINISH_REASON_ALIASES.get(normalized, "error")


class ToolCallAccumulator:
    """Collect provider tool-call fragments and validate them at turn end."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def add(self, delta: ToolCallDelta) -> None:
        call = self._calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
        call["id"] += delta.id_delta
        call["name"] += delta.name_delta
        call["arguments"] += delta.arguments_delta

    def finalize(self) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            call = self._calls[index]
            name = call["name"]
            if not name:
                raise StructuredStreamProtocolError(
                    "tool_call_missing_name",
                    f"tool call at index {index} has no name",
                    index=index,
                )
            raw_arguments = call["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise StructuredStreamProtocolError(
                    "tool_call_invalid_json",
                    f"tool call at index {index} has invalid JSON arguments",
                    index=index,
                ) from error
            if not isinstance(arguments, dict):
                raise StructuredStreamProtocolError(
                    "tool_call_arguments_not_object",
                    f"tool call at index {index} arguments must be a JSON object",
                    index=index,
                )
            calls.append(ToolCall(id=call["id"], name=name, arguments=arguments))
        return tuple(calls)


_DSML_PREFIXES = (
    "<｜DSML｜",
    "<｜｜DSML｜｜",
    "<function_calls",
    "<function_call",
    "<invoke",
    "<parameter",
    "<param",
    "<operation",
    "<tool_calls",
    "<tool_call",
    "[TOOL_CALL]",
    "```tool_call",
    "<function=",
    "<parameter=",
    "<read_file",
    "<write_file",
    "<list_files",
    "<search_files",
    "<vision_analyze",
    "<camera_capture",
    "<multi_search",
    "<web_browse",
    "<web_search",
    "<search_cn",
    "<shell_command",
    "<python_executor",
    "<document_reader",
    "<save_memory",
    "<recall_memory",
    "<search_memory",
    "<nudge",
    "<get_hardware_info",
    "<control_gpio",
    "<read_sensor",
    "<wolfram_query",
    "<analyze_code",
    "<run_code",
    "<edit_code",
    "<create_file",
    "<arg",
    "<calculator",
    "<get_weather",
    "<get_current_time",
    "<agnes_image",
    "<agnes_video",
    "<agnes_tts",
)
_DSML_START_PATTERN = re.compile(
    r"<｜{1,2}DSML｜{1,2}(?:tool_calls?|function_calls?|invoke|parameter)\b"
    r"|<(?:tool_calls?|function_calls?|invoke|parameters?|param|operation|tool_call)\b"
    r"|\[TOOL_CALL\]"
    r"|```tool_call\b"
    r"|<(?:function|parameter)="
    r"|<(?:read_file|write_file|list_files|search_files|vision_analyze|camera_capture|"
    r"multi_search|web_browse|web_search|search_cn|shell_command|python_executor|document_reader|"
    r"save_memory|recall_memory|search_memory|nudge|get_hardware_info|control_gpio|read_sensor|"
    r"wolfram_query|analyze_code|run_code|edit_code|create_file|arg|calculator|get_weather|"
    r"get_current_time|agnes_image|agnes_video|agnes_tts)\b",
    re.IGNORECASE,
)
_DSML_END_PATTERNS = (
    re.compile(r"\[/TOOL_CALL\]", re.IGNORECASE),
    re.compile(r"```"),
    re.compile(r"</function>", re.IGNORECASE),
)


class DSMLPreviewFilter:
    """Stream normal text while retaining and suppressing tool-protocol blocks."""

    def __init__(self, max_buffer_chars: int = 64 * 1024) -> None:
        if max_buffer_chars <= 0:
            raise ValueError("max_buffer_chars must be positive")
        self._max_buffer_chars = max_buffer_chars
        self._raw_parts: list[str] = []
        self._raw_size = 0
        self._pending = ""
        self._suppressing = False
        self._end_patterns: tuple[re.Pattern[str], ...] = ()
        self._failed = False

    def feed(self, chunk: str) -> str:
        if not isinstance(chunk, str):
            raise TypeError("chunk must be a string")
        if self._failed:
            return ""
        if self._raw_size + len(chunk) > self._max_buffer_chars:
            self._fail_buffer_limit()
        self._raw_parts.append(chunk)
        self._raw_size += len(chunk)
        self._pending += chunk
        return self._drain()

    def finish(self) -> str:
        if self._failed:
            self._raise_buffer_limit()
        if not self._suppressing:
            self._pending = ""
        return "".join(self._raw_parts)

    def _drain(self) -> str:
        visible: list[str] = []
        while self._pending:
            if self._suppressing:
                end = self._find_end()
                if end is None:
                    self._trim_suppressed_pending()
                    break
                self._pending = self._pending[end:]
                self._suppressing = False
                self._end_patterns = ()
                continue

            start = _DSML_START_PATTERN.search(self._pending)
            if start is not None:
                visible.append(self._pending[: start.start()])
                opener = start.group(0).lower()
                self._pending = self._pending[start.end() :]
                self._suppressing = True
                self._end_patterns = self._closing_patterns(opener)
                continue

            hold = self._possible_prefix_length(self._pending)
            if hold:
                visible.append(self._pending[:-hold])
                self._pending = self._pending[-hold:]
                break
            else:
                visible.append(self._pending)
                self._pending = ""
        return "".join(visible)

    def _find_end(self) -> int | None:
        matches = [pattern.search(self._pending) for pattern in self._end_patterns]
        matches = [match for match in matches if match is not None]
        if not matches:
            return None
        first = min(matches, key=lambda match: match.start())
        return first.end()

    def _trim_suppressed_pending(self) -> None:
        longest_ending = max((len(pattern.pattern) for pattern in self._end_patterns), default=64)
        if len(self._pending) > longest_ending:
            self._pending = self._pending[-longest_ending:]

    @staticmethod
    def _possible_prefix_length(text: str) -> int:
        lowered = text.lower()
        for length in range(min(len(text), max(map(len, _DSML_PREFIXES)) - 1), 0, -1):
            suffix = lowered[-length:]
            if any(prefix.lower().startswith(suffix) for prefix in _DSML_PREFIXES):
                return length
        return 0

    @staticmethod
    def _closing_patterns(opener: str) -> tuple[re.Pattern[str], ...]:
        if opener.startswith("[tool_call]"):
            return (_DSML_END_PATTERNS[0],)
        if opener.startswith("```tool_call"):
            return (_DSML_END_PATTERNS[1],)
        if opener.startswith("<function=") or opener.startswith("<parameter="):
            return (_DSML_END_PATTERNS[2],)
        name_match = re.search(r"(?:dsml｜{1,2})?([a-z_]+)", opener)
        if not name_match:
            return (re.compile(r"</tool_call>", re.IGNORECASE),)
        name = name_match.group(1)
        if "dsml" in opener:
            escaped = re.escape(name)
            return (
                re.compile(rf"</｜{{1,2}}DSML｜{{1,2}}{escaped}>", re.IGNORECASE),
                re.compile(rf"</{escaped}>", re.IGNORECASE),
            )
        return (re.compile(rf"</{re.escape(name)}>", re.IGNORECASE),)

    def _fail_buffer_limit(self) -> None:
        self._failed = True
        self._raw_parts.clear()
        self._raw_size = 0
        self._pending = ""
        self._raise_buffer_limit()

    def _raise_buffer_limit(self) -> None:
        raise StructuredStreamProtocolError(
            "dsml_preview_buffer_exceeded",
            f"DSML preview buffer exceeded {self._max_buffer_chars} characters",
        )
