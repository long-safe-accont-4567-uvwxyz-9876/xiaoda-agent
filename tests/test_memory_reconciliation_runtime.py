from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from db.db_memory_reconciliation import (
    ReconciliationDecisionInput,
    ReconciliationJobClaim,
    ReconciliationRepository,
)
from memory._memory_encoder import MemoryEncoder
from memory.reconciliation_models import DecisionValidationContext, MemoryIdentity
from memory.reconciliation_service import (
    build_decision_provider,
    build_reconciliation_worker,
    run_pending_once,
)
from memory.reconciliation_worker import ReconciliationMode


def _decision_input() -> ReconciliationDecisionInput:
    claim = ReconciliationJobClaim(
        job_id=7,
        candidate_memory_id=70,
        user_id="u",
        agent_id="a",
        lease_token="lease",
        lease_expires=100.0,
        retry_count=0,
        candidate_expected_version=1,
        candidate_expected_status="active",
    )
    candidate = {
        "id": 70,
        "summary": "python project",
        "user_id": "u",
        "agent_id": "a",
        "is_raw": 0,
        "status": "active",
        "version": 1,
    }
    target = {
        "id": 10,
        "summary": "older python project",
        "user_id": "u",
        "agent_id": "a",
        "is_raw": 0,
        "status": "active",
        "version": 3,
    }
    context = DecisionValidationContext(
        job_id=7,
        user_id="u",
        agent_id="a",
        candidate=MemoryIdentity(
            memory_id=70,
            user_id="u",
            agent_id="a",
            is_raw=False,
            status="active",
            version=1,
        ),
        targets={
            10: MemoryIdentity(
                memory_id=10,
                user_id="u",
                agent_id="a",
                is_raw=False,
                status="active",
                version=3,
            )
        },
    )
    return ReconciliationDecisionInput(
        claim=claim,
        candidate=candidate,
        candidates=(target,),
        context=context,
        candidate_expected_version=1,
        candidate_expected_status="active",
    )


@pytest.mark.asyncio
async def test_decision_provider_sends_strict_json_through_existing_free_model() -> None:
    response = json.dumps(
        {
            "job_id": 7,
            "action": "update",
            "target_ids": [10],
            "canonical_summary": "canonical",
            "confidence": 0.9,
            "reason": "same fact",
        }
    )
    distiller = SimpleNamespace(
        _call_free_model=AsyncMock(return_value=response),
        router=None,
    )

    provider = build_decision_provider(distiller)
    assert provider is not None
    assert await provider(_decision_input()) == response

    call = distiller._call_free_model.await_args
    messages = call.args[0]
    prompt = json.loads(messages[-1]["content"])
    assert prompt["input"]["job_id"] == 7
    assert prompt["input"]["candidate"]["summary"] == "python project"
    assert [item["id"] for item in prompt["input"]["candidates"]] == [10]
    assert prompt["output_schema"]["additionalProperties"] is False
    assert call.kwargs == {"temperature": 0.0, "max_tokens": 2048}


@pytest.mark.asyncio
async def test_decision_provider_falls_back_to_memory_encoding_router() -> None:
    response = '{"job_id":7}'
    router = SimpleNamespace(route=AsyncMock(return_value=response))
    distiller = SimpleNamespace(
        _call_free_model=AsyncMock(return_value=None),
        router=router,
    )

    provider = build_decision_provider(distiller)
    assert provider is not None
    assert await provider(_decision_input()) == response
    assert router.route.await_args.kwargs["task_type"] == "memory_encoding"


@pytest.mark.asyncio
async def test_run_pending_without_provider_leaves_job_unclaimed() -> None:
    repository = AsyncMock()
    manager = SimpleNamespace(
        db=SimpleNamespace(reconciliation=repository),
        memory=SimpleNamespace(reconciliation=repository),
        distiller=None,
    )

    result = await run_pending_once(manager, user_id="u", agent_id="a")

    assert result.status == "deferred"
    repository.claim_pending.assert_not_awaited()


def test_enforce_without_allowed_actions_fails_closed_to_shadow(monkeypatch) -> None:
    import config
    from memory.reconciliation_policy import configured_policy

    monkeypatch.setattr(config, "MEMORY_RECONCILIATION_MODE", "enforce")
    monkeypatch.setattr(config, "MEMORY_RECONCILIATION_ALLOWED_ACTIONS", "", raising=False)
    monkeypatch.setattr(config, "KG_V2_ENABLED", False)
    effective_mode, actions = configured_policy()
    worker = build_reconciliation_worker(
        SimpleNamespace(reconciliation=AsyncMock()),
        AsyncMock(),
        user_id="u",
        agent_id="a",
    )

    assert effective_mode == "shadow"
    assert actions == set()
    assert worker._mode is ReconciliationMode.SHADOW
    assert worker._allowed_actions == frozenset()


def test_enforce_kg_guard_reads_runtime_config_and_cannot_be_overridden(monkeypatch) -> None:
    import config

    monkeypatch.setattr(config, "KG_V2_ENABLED", True)
    db = SimpleNamespace(reconciliation=AsyncMock())

    with pytest.raises(RuntimeError, match="KG v2"):
        build_reconciliation_worker(
            db,
            AsyncMock(),
            user_id="u",
            agent_id="a",
            mode=ReconciliationMode.ENFORCE,
            kg_v2_enabled=False,
        )


async def _runtime_repo() -> tuple[aiosqlite.Connection, ReconciliationRepository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(
        """
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            is_raw INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
            summary TEXT NOT NULL, superseded_by INTEGER
        )
        """
    )
    await conn.executemany(
        "INSERT INTO episodic_memories VALUES (?, 'u', 'a', 0, 'active', 1, ?, NULL)",
        [(10, "older python project"), (70, "python project")],
    )
    await conn.commit()
    repository = ReconciliationRepository(conn)
    await repository.create_schema()
    return conn, repository


@pytest.mark.asyncio
async def test_encoder_enqueue_spawns_one_shadow_run_and_records_proposal(monkeypatch) -> None:
    import config

    monkeypatch.setattr(config, "MEMORY_RECONCILIATION_MODE", "shadow")
    conn, repository = await _runtime_repo()

    async def decide(messages, **_kwargs):
        prompt = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "job_id": prompt["input"]["job_id"],
                "action": "update",
                "target_ids": [10],
                "canonical_summary": "canonical python project",
                "confidence": 0.9,
                "reason": "same project",
            }
        )

    manager = SimpleNamespace(
        db=SimpleNamespace(reconciliation=repository),
        memory=SimpleNamespace(reconciliation=repository),
        distiller=SimpleNamespace(_call_free_model=decide, router=None),
    )
    encoder = MemoryEncoder(manager)
    try:
        encoder._schedule_reconciliation_candidate(
            70, 1, SimpleNamespace(user_id="u", agent_id="a")
        )

        row = None
        for _ in range(100):
            row = await (
                await conn.execute(
                    "SELECT status FROM memory_reconciliation_jobs WHERE candidate_memory_id=70"
                )
            ).fetchone()
            if row is not None and row["status"] == "shadow":
                break
            await asyncio.sleep(0.01)

        assert row is not None and row["status"] == "shadow"
        action = await (
            await conn.execute(
                "SELECT proposed_action, executed FROM memory_reconciliation_actions"
            )
        ).fetchone()
        assert dict(action) == {"proposed_action": "update", "executed": 0}
        candidate = await (
            await conn.execute("SELECT status FROM episodic_memories WHERE id=70")
        ).fetchone()
        assert candidate["status"] == "active"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_reconciliation_spawn_failure_does_not_escape_distill_flow() -> None:
    repository = SimpleNamespace(
        register_candidate=AsyncMock(side_effect=RuntimeError("db unavailable"))
    )
    manager = SimpleNamespace(memory=SimpleNamespace(reconciliation=repository))

    MemoryEncoder(manager)._schedule_reconciliation_candidate(
        70, 1, SimpleNamespace(user_id="u", agent_id="a")
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    repository.register_candidate.assert_awaited_once()
