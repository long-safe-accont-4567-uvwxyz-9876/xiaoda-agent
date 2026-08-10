from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, Sequence

_TOOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_MAX_TASK_LENGTH = 100_000
_MAX_CONTEXT_LENGTH = 200_000
_MAX_ITEMS = 256
_MAX_ITEM_LENGTH = 512
_MAX_TIMEOUT_SECONDS = 600.0


class InvalidSubAgentInvocation(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _clean_text(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidSubAgentInvocation(field_name, "must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise InvalidSubAgentInvocation(field_name, "must not be empty")
    if len(cleaned) > max_length:
        raise InvalidSubAgentInvocation(field_name, f"must not exceed {max_length} characters")
    return cleaned


def _clean_sequence(value: Sequence[str], field_name: str, *, paths: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise InvalidSubAgentInvocation(field_name, "must be a sequence of strings")
    if len(value) > _MAX_ITEMS:
        raise InvalidSubAgentInvocation(field_name, f"must not contain more than {_MAX_ITEMS} items")
    cleaned_items: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise InvalidSubAgentInvocation(field_name, "must contain only strings")
        cleaned = item.strip()
        if not cleaned or len(cleaned) > _MAX_ITEM_LENGTH:
            raise InvalidSubAgentInvocation(field_name, "contains an empty or oversized item")
        if "\x00" in cleaned:
            raise InvalidSubAgentInvocation(field_name, "must not contain NUL")
        if paths:
            if cleaned.startswith(("/", "\\", "~")) or "\\" in cleaned:
                raise InvalidSubAgentInvocation(field_name, "must contain virtual relative patterns only")
            if len(cleaned) >= 3 and cleaned[0].isalpha() and cleaned[1:3] == ":/":
                raise InvalidSubAgentInvocation(field_name, "must contain virtual relative patterns only")
            if any(part == ".." for part in cleaned.split("/")):
                raise InvalidSubAgentInvocation(field_name, "must not escape its virtual root")
        elif not _TOOL_PATTERN.fullmatch(cleaned):
            raise InvalidSubAgentInvocation(field_name, f"contains invalid tool name {cleaned!r}")
        if cleaned not in seen:
            seen.add(cleaned)
            cleaned_items.append(cleaned)
    return tuple(cleaned_items)


@dataclass(frozen=True, slots=True)
class SubAgentInvocation:
    target: str
    task: str
    context: str = ""
    allowed_tools: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    permission_mode: Literal["default", "dev", "strict"] = "default"
    timeout_seconds: float = 180.0
    request_id: str | None = None

    def __post_init__(self) -> None:
        target = _clean_text(self.target, "target", 64)
        if not target.isidentifier() or target != target.lower():
            raise InvalidSubAgentInvocation("target", "must be a lowercase agent identifier")
        task = _clean_text(self.task, "task", _MAX_TASK_LENGTH)
        if not isinstance(self.context, str):
            raise InvalidSubAgentInvocation("context", "must be a string")
        context = self.context.strip()
        if len(context) > _MAX_CONTEXT_LENGTH:
            raise InvalidSubAgentInvocation("context", f"must not exceed {_MAX_CONTEXT_LENGTH} characters")
        if not isinstance(self.permission_mode, str) or self.permission_mode not in {"default", "dev", "strict"}:
            raise InvalidSubAgentInvocation("permission_mode", "must be default, dev, or strict")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise InvalidSubAgentInvocation("timeout_seconds", "must be a number")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
            raise InvalidSubAgentInvocation("timeout_seconds", f"must be within (0, {_MAX_TIMEOUT_SECONDS}]")
        if self.request_id is not None and not isinstance(self.request_id, str):
            raise InvalidSubAgentInvocation("request_id", "must be a string")
        request_id = self.request_id.strip() if self.request_id is not None else None
        if request_id is not None and (not request_id or len(request_id) > 128 or "\x00" in request_id):
            raise InvalidSubAgentInvocation("request_id", "must be a non-empty safe identifier")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "allowed_tools", _clean_sequence(self.allowed_tools, "allowed_tools"))
        object.__setattr__(self, "allowed_paths", _clean_sequence(self.allowed_paths, "allowed_paths", paths=True))
        object.__setattr__(self, "forbidden_paths", _clean_sequence(self.forbidden_paths, "forbidden_paths", paths=True))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "request_id", request_id)


@dataclass(frozen=True, slots=True)
class SubAgentInvocationResult:
    target: str
    status: Literal["completed", "unavailable", "timeout", "cancelled", "failed"]
    final_report: str = ""
    error_code: str | None = None
    error_message: str | None = None
    elapsed_ms: int | None = None

    def __post_init__(self) -> None:
        target = _clean_text(self.target, "target", 64)
        if not target.isidentifier() or target != target.lower():
            raise InvalidSubAgentInvocation("target", "must be a lowercase agent identifier")
        if self.status not in {"completed", "unavailable", "timeout", "cancelled", "failed"}:
            raise InvalidSubAgentInvocation("status", "contains an unsupported status")
        if not isinstance(self.final_report, str):
            raise InvalidSubAgentInvocation("final_report", "must be a string")
        final_report = self.final_report.strip()
        if self.status == "completed" and not final_report:
            raise InvalidSubAgentInvocation("final_report", "must not be empty when completed")
        if self.status != "completed" and final_report:
            raise InvalidSubAgentInvocation("final_report", "must be empty unless completed")
        for field_name in ("error_code", "error_message"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise InvalidSubAgentInvocation(field_name, "must be a non-empty string")
        if self.elapsed_ms is not None and (isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int) or self.elapsed_ms < 0):
            raise InvalidSubAgentInvocation("elapsed_ms", "must be a non-negative integer")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "final_report", final_report)
        if self.error_code is not None:
            object.__setattr__(self, "error_code", self.error_code.strip())
        if self.error_message is not None:
            object.__setattr__(self, "error_message", self.error_message.strip())

    @classmethod
    def completed(cls, target: str, final_report: str, elapsed_ms: int | None = None) -> SubAgentInvocationResult:
        return cls(target=target, status="completed", final_report=final_report, elapsed_ms=elapsed_ms)
