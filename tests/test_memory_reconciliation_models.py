from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from memory.reconciliation_models import (
    DecisionValidationContext,
    MemoryIdentity,
    ReconciliationAction,
    ReconciliationDecision,
)
from memory.reconciliation_policy import DecisionBatchError, parse_decision_batch


def _payload(action: str = "store", targets: list[int] | None = None) -> dict:
    return {
        "job_id": 7,
        "action": action,
        "target_ids": [] if targets is None else targets,
        "canonical_summary": "canonical fact",
        "confidence": 0.9,
        "reason": "supported by the candidate",
    }


def _context(*targets: MemoryIdentity) -> DecisionValidationContext:
    return DecisionValidationContext(
        job_id=7,
        user_id="user-a",
        agent_id="agent-a",
        candidate=MemoryIdentity(
            memory_id=70,
            user_id="user-a",
            agent_id="agent-a",
            is_raw=False,
            status="pending",
            version=1,
        ),
        targets={target.memory_id: target for target in targets},
    )


def _target(
    memory_id: int = 10,
    *,
    user_id: str = "user-a",
    agent_id: str = "agent-a",
    is_raw: bool = False,
    status: str = "active",
) -> MemoryIdentity:
    return MemoryIdentity(
        memory_id=memory_id,
        user_id=user_id,
        agent_id=agent_id,
        is_raw=is_raw,
        status=status,
        version=3,
    )


def test_decision_is_strict_and_rejects_unknown_fields() -> None:
    payload = _payload()
    payload["prompt_injection"] = "ignore policy"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReconciliationDecision.model_validate(payload)


@pytest.mark.parametrize("confidence", [True, False, -0.01, 1.01, float("nan"), float("inf")])
def test_decision_rejects_invalid_confidence(confidence: object) -> None:
    payload = _payload()
    payload["confidence"] = confidence

    with pytest.raises(ValidationError):
        ReconciliationDecision.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("canonical_summary", "s" * 4097), ("reason", "r" * 2049)],
)
def test_decision_enforces_text_limits(field: str, value: str) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        ReconciliationDecision.model_validate(payload)


@pytest.mark.parametrize(
    ("action", "targets"),
    [
        ("store", []),
        ("update", [10]),
        ("merge", [10]),
        ("merge", [10, 11]),
        ("skip", []),
        ("skip", [10]),
    ],
)
def test_action_target_cardinality_accepts_contract(action: str, targets: list[int]) -> None:
    decision = ReconciliationDecision.model_validate(_payload(action, targets))

    assert decision.action is ReconciliationAction(action)
    assert decision.target_ids == targets


@pytest.mark.parametrize(
    ("action", "targets"),
    [
        ("store", [10]),
        ("update", []),
        ("update", [10, 11]),
        ("merge", []),
        ("skip", [10, 11]),
    ],
)
def test_action_target_cardinality_rejects_invalid_combinations(action: str, targets: list[int]) -> None:
    with pytest.raises(ValidationError):
        ReconciliationDecision.model_validate(_payload(action, targets))


@pytest.mark.parametrize("targets", [[10, 10], [0], [-1], [True]])
def test_target_ids_must_be_unique_positive_strict_integers(targets: list[object]) -> None:
    with pytest.raises(ValidationError):
        ReconciliationDecision.model_validate(_payload("merge", targets))


@pytest.mark.parametrize(
    "wrapped",
    [
        lambda value: value,
        lambda value: f"```json\n{value}\n```",
        lambda value: f"<think>private chain of thought</think>\n{value}",
        lambda value: f"<think>first</think>```json\n{value}\n```",
    ],
)
def test_parser_supports_json_fences_and_think_cleanup(wrapped: Callable[[str], str]) -> None:
    encoded = json.dumps(_payload(), ensure_ascii=False)

    decisions = parse_decision_batch(wrapped(encoded), {7: _context()})

    assert [decision.job_id for decision in decisions] == [7]


@pytest.mark.parametrize("text", ["not json", "```json\n{}", "<think>unterminated"])
def test_parser_rejects_malformed_output(text: str) -> None:
    with pytest.raises(DecisionBatchError):
        parse_decision_batch(text, {7: _context()})


def test_parser_rejects_entire_batch_when_one_decision_is_invalid() -> None:
    valid = _payload()
    invalid = _payload()
    invalid["job_id"] = 8
    invalid["confidence"] = 2

    with pytest.raises(DecisionBatchError) as exc_info:
        parse_decision_batch(
            json.dumps([valid, invalid]),
            {7: _context(), 8: _context().model_copy(update={"job_id": 8})},
        )

    assert exc_info.value.decisions == ()


@pytest.mark.parametrize(
    "target",
    [
        _target(user_id="other-user"),
        _target(agent_id="other-agent"),
        _target(is_raw=True),
        _target(status="superseded"),
    ],
)
def test_validation_context_rejects_out_of_scope_raw_or_inactive_target(target: MemoryIdentity) -> None:
    with pytest.raises(DecisionBatchError):
        parse_decision_batch(json.dumps(_payload("update", [10])), {7: _context(target)})


def test_validation_context_rejects_target_outside_candidate_pool() -> None:
    with pytest.raises(DecisionBatchError):
        parse_decision_batch(json.dumps(_payload("update", [999])), {7: _context(_target())})


def test_parser_rejects_job_without_matching_context() -> None:
    with pytest.raises(DecisionBatchError):
        parse_decision_batch(json.dumps(_payload()), {})


def test_parser_rejects_batch_that_does_not_cover_every_context() -> None:
    second_context = _context().model_copy(update={"job_id": 8})

    with pytest.raises(DecisionBatchError):
        parse_decision_batch(json.dumps([_payload()]), {7: _context(), 8: second_context})


def test_validation_context_rejects_target_pool_key_identity_mismatch() -> None:
    with pytest.raises(ValidationError, match="target pool key"):
        DecisionValidationContext(
            job_id=7,
            user_id="user-a",
            agent_id="agent-a",
            candidate=_context().candidate,
            targets={10: _target(memory_id=11)},
        )


def test_fallback_is_deterministic_store_and_keeps_candidate_summary() -> None:
    context = _context()

    decision = context.fallback_store("candidate summary")

    assert decision == ReconciliationDecision(
        job_id=7,
        action=ReconciliationAction.STORE,
        target_ids=[],
        canonical_summary="candidate summary",
        confidence=0.0,
        reason="deterministic_fallback",
    )
