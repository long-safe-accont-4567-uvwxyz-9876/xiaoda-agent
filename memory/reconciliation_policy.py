from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from memory.reconciliation_models import (
    DecisionValidationContext,
    ReconciliationAction,
    ReconciliationDecision,
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.IGNORECASE | re.DOTALL)


class DecisionBatchError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.decisions: tuple[ReconciliationDecision, ...] = ()


def configured_policy() -> tuple[str, set[ReconciliationAction]]:
    """Return the effective runtime mode and allowed action set."""
    try:
        import config

        requested = str(getattr(config, "MEMORY_RECONCILIATION_MODE", "shadow")).lower()
        raw_actions = getattr(config, "MEMORY_RECONCILIATION_ALLOWED_ACTIONS", "")
    except ImportError:
        requested = "shadow"
        raw_actions = ""
    values = raw_actions.split(",") if isinstance(raw_actions, str) else (raw_actions or ())
    actions = {
        ReconciliationAction(str(getattr(value, "value", value)).strip().lower())
        for value in values
        if str(getattr(value, "value", value)).strip()
    }
    mode = "enforce" if requested == "enforce" and actions else "shadow"
    return mode, actions


def _clean_output(text: str) -> str:
    if not isinstance(text, str):
        raise DecisionBatchError("decision output must be text")
    cleaned = _THINK_BLOCK.sub("", text).strip()
    if "<think" in cleaned.lower() or "</think>" in cleaned.lower():
        raise DecisionBatchError("malformed think block")
    fence = _FENCE.fullmatch(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    elif cleaned.startswith("```") or cleaned.endswith("```"):
        raise DecisionBatchError("malformed JSON fence")
    return cleaned


def _load_payload(text: str) -> list[Any]:
    try:
        payload = json.loads(_clean_output(text), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, DecisionBatchError):
            raise
        raise DecisionBatchError("invalid decision JSON") from exc
    return payload if isinstance(payload, list) else [payload]


def parse_decision_batch(
    text: str,
    contexts: Mapping[int, DecisionValidationContext],
) -> list[ReconciliationDecision]:
    payloads = _load_payload(text)
    if not payloads:
        raise DecisionBatchError("decision batch must not be empty")

    decisions: list[ReconciliationDecision] = []
    seen_jobs: set[int] = set()
    try:
        for payload in payloads:
            decision = ReconciliationDecision.model_validate(payload)
            if decision.job_id in seen_jobs:
                raise ValueError("duplicate job decision")
            context = contexts.get(decision.job_id)
            if context is None:
                raise ValueError("missing validation context")
            context.validate_decision(decision)
            seen_jobs.add(decision.job_id)
            decisions.append(decision)
    except (ValidationError, ValueError, TypeError) as exc:
        raise DecisionBatchError("invalid decision batch") from exc
    if seen_jobs != set(contexts):
        raise DecisionBatchError("decision batch does not cover every validation context")
    return decisions
