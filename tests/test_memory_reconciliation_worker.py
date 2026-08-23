from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from db.db_memory_reconciliation import (
    ReconciliationDecisionInput,
    ReconciliationJobClaim,
    ReconciliationRepository,
    RepositoryConflictError,
)
from memory.reconciliation_models import DecisionValidationContext, MemoryIdentity, ReconciliationAction
from memory.reconciliation_worker import ReconciliationMode, ReconciliationWorker, run_forever


def _claim(*, retry_count: int = 0) -> ReconciliationJobClaim:
    return ReconciliationJobClaim(
        job_id=7,
        candidate_memory_id=70,
        user_id="u",
        agent_id="a",
        lease_token="lease-7",
        lease_expires=100,
        retry_count=retry_count,
        candidate_expected_version=1,
        candidate_expected_status="pending",
    )


def _decision_input(*, retry_count: int = 0) -> ReconciliationDecisionInput:
    claim = _claim(retry_count=retry_count)
    candidate = {
        "id": 70,
        "user_id": "u",
        "agent_id": "a",
        "is_raw": 0,
        "status": "pending",
        "version": 1,
        "summary": "candidate summary",
    }
    target = {
        "id": 10,
        "user_id": "u",
        "agent_id": "a",
        "is_raw": 0,
        "status": "active",
        "version": 3,
        "summary": "existing summary",
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
            status="pending",
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
        candidate_expected_status="pending",
    )


def _json(action: str = "update", targets: list[int] | None = None) -> str:
    return json.dumps(
        {
            "job_id": 7,
            "action": action,
            "target_ids": [10] if targets is None else targets,
            "canonical_summary": "canonical",
            "confidence": 0.9,
            "reason": "provider reason must not be logged",
        }
    )


def _repo(*, retry_count: int = 0):
    repo = AsyncMock()
    claim = _claim(retry_count=retry_count)
    repo.claim_pending.return_value = claim
    repo.load_decision_input.return_value = _decision_input(retry_count=retry_count)
    repo.apply_action.return_value = 101
    repo.record_shadow.return_value = 102
    repo.fail_job.return_value = True
    repo.release_lease.return_value = True
    return repo


@pytest.mark.asyncio
async def test_run_once_returns_idle_when_no_job_is_claimed() -> None:
    repo = _repo()
    repo.claim_pending.return_value = None
    provider = AsyncMock()
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    result = await worker.run_once()

    assert result.status == "idle"
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_receives_candidate_and_candidate_pool() -> None:
    repo = _repo()
    provider = AsyncMock(return_value=_json("store", []))
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.ENFORCE,
        allowed_actions={ReconciliationAction.STORE},
    )

    await worker.run_once()

    decision_input = provider.await_args.args[0]
    assert decision_input.candidate["id"] == 70
    assert [candidate["id"] for candidate in decision_input.candidates] == [10]


@pytest.mark.asyncio
async def test_shadow_records_proposal_and_never_applies() -> None:
    repo = _repo()
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.SHADOW,
        allowed_actions={ReconciliationAction.UPDATE},
    )

    result = await worker.run_once()

    assert result.status == "shadow"
    repo.record_shadow.assert_awaited_once()
    repo.apply_action.assert_not_awaited()
    repo.complete_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_applies_allowed_action_with_target_versions() -> None:
    repo = _repo()
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.ENFORCE,
        allowed_actions={ReconciliationAction.UPDATE},
    )

    result = await worker.run_once()

    assert result.status == "applied"
    assert result.action is ReconciliationAction.UPDATE
    assert repo.apply_action.await_args.kwargs["expected_versions"] == {10: 3}
    assert repo.apply_action.await_args.kwargs["candidate_expected_version"] == 1
    assert repo.apply_action.await_args.kwargs["candidate_expected_status"] == "pending"
    repo.record_shadow.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_disallowed_action_degrades_to_shadow_proposal() -> None:
    repo = _repo()
    provider = AsyncMock(return_value=_json("merge", [10]))
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.ENFORCE,
        allowed_actions={ReconciliationAction.STORE},
    )

    result = await worker.run_once()

    assert result.status == "shadow"
    repo.record_shadow.assert_awaited_once()
    repo.apply_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_count", "expected_delay"),
    [(0, 30), (1, 120), (2, 600)],
)
async def test_provider_failure_uses_deterministic_retry_policy(retry_count: int, expected_delay: int) -> None:
    repo = _repo(retry_count=retry_count)
    provider = AsyncMock(side_effect=RuntimeError("secret provider response"))
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    result = await worker.run_once()

    assert result.status == "retry"
    assert repo.fail_job.await_args.kwargs["retry_delay"] == expected_delay
    assert repo.fail_job.await_args.kwargs["error"] == "decision_provider_failed"
    repo.apply_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_failure_is_retried_without_partial_execution() -> None:
    repo = _repo()
    provider = AsyncMock(return_value="not json")
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    result = await worker.run_once()

    assert result.status == "retry"
    assert repo.fail_job.await_args.kwargs["error"] == "decision_parse_failed"
    repo.apply_action.assert_not_awaited()
    repo.record_shadow.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_candidate_pool_skips_provider_and_uses_fallback_store() -> None:
    repo = _repo()
    decision_input = repo.load_decision_input.return_value
    repo.load_decision_input.return_value = ReconciliationDecisionInput(
        claim=decision_input.claim,
        candidate=decision_input.candidate,
        candidates=(),
        context=decision_input.context,
        candidate_expected_version=decision_input.candidate_expected_version,
        candidate_expected_status=decision_input.candidate_expected_status,
    )
    provider = AsyncMock()
    worker = ReconciliationWorker(
        repo, provider, user_id="u", agent_id="a", mode=ReconciliationMode.SHADOW,
    )

    result = await worker.run_once()

    assert result.status == "fallback_shadow"
    provider.assert_not_awaited()
    decision = repo.record_shadow.await_args.args[2]
    assert decision.action is ReconciliationAction.STORE


@pytest.mark.asyncio
async def test_exhausted_provider_failures_apply_fallback_store() -> None:
    repo = _repo(retry_count=3)
    provider = AsyncMock(side_effect=RuntimeError("still down"))
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.ENFORCE,
        allowed_actions={ReconciliationAction.STORE},
    )

    result = await worker.run_once()

    assert result.status == "fallback_applied"
    decision = repo.apply_action.await_args.args[2]
    assert decision.action is ReconciliationAction.STORE
    assert decision.target_ids == []
    assert decision.canonical_summary == "candidate summary"
    assert decision.reason == "deterministic_fallback"
    repo.fail_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_fallback_obeys_shadow_mode() -> None:
    repo = _repo(retry_count=3)
    provider = AsyncMock(side_effect=RuntimeError("still down"))
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.SHADOW,
    )

    result = await worker.run_once()

    assert result.status == "fallback_shadow"
    repo.record_shadow.assert_awaited_once()
    repo.apply_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_method", "mode", "allowed_actions", "error_code"),
    [
        ("load_decision_input", ReconciliationMode.SHADOW, set(), "decision_input_load_failed"),
        ("record_shadow", ReconciliationMode.SHADOW, set(), "proposal_record_failed"),
        (
            "apply_action",
            ReconciliationMode.ENFORCE,
            {ReconciliationAction.UPDATE},
            "action_apply_failed",
        ),
    ],
)
async def test_non_cancel_stage_failures_immediately_schedule_retry(
    failing_method: str,
    mode: ReconciliationMode,
    allowed_actions: set[ReconciliationAction],
    error_code: str,
) -> None:
    repo = _repo()
    getattr(repo, failing_method).side_effect = RepositoryConflictError("candidate changed")
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=mode,
        allowed_actions=allowed_actions,
    )

    result = await worker.run_once()

    assert result.status == "retry"
    repo.fail_job.assert_awaited_once_with(
        7,
        "lease-7",
        error=error_code,
        retry_delay=30.0,
    )


@pytest.mark.asyncio
async def test_fail_job_failure_logs_and_reraises_original_stage_error(caplog) -> None:
    repo = _repo()
    original = RepositoryConflictError("candidate changed during inference")
    retry_failure = RuntimeError("database unavailable")
    repo.apply_action.side_effect = original
    repo.fail_job.side_effect = retry_failure
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(
        repo,
        provider,
        user_id="u",
        agent_id="a",
        mode=ReconciliationMode.ENFORCE,
        allowed_actions={ReconciliationAction.UPDATE},
    )

    with caplog.at_level(logging.ERROR, logger="memory.reconciliation_worker"):
        with pytest.raises(RepositoryConflictError, match="candidate changed") as exc_info:
            await worker.run_once()

    assert exc_info.value is original
    assert exc_info.value.__cause__ is retry_failure
    assert "memory.reconciliation_worker.fail_job_failed" in caplog.text
    assert "candidate changed during inference" not in caplog.text


@pytest.mark.asyncio
async def test_candidate_rewrite_during_provider_causes_immediate_retry_without_action() -> None:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    repo = ReconciliationRepository(conn)
    try:
        await conn.execute(
            """
            CREATE TABLE episodic_memories (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                is_raw INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                summary TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                importance REAL NOT NULL DEFAULT 0.5, phase TEXT NOT NULL DEFAULT 'new',
                stability REAL NOT NULL DEFAULT 1.0, rc INTEGER NOT NULL DEFAULT 0,
                superseded_by INTEGER
            )
            """
        )
        await conn.executemany(
            "INSERT INTO episodic_memories VALUES (?, 'u', 'a', 0, ?, ?, ?, '{}', .5, 'new', 1, 0, NULL)",
            [
                (70, "pending", 1, "candidate"),
                (10, "active", 1, "related candidate history"),
            ],
        )
        await conn.commit()
        await repo.create_schema()
        await repo.enqueue(70, user_id="u", agent_id="a", now=1)

        async def rewrite_candidate(decision_input: ReconciliationDecisionInput) -> str:
            await conn.execute(
                "UPDATE episodic_memories SET summary='concurrent', version=version+1 WHERE id=70"
            )
            await conn.commit()
            payload = json.loads(_json("store", []))
            payload["job_id"] = decision_input.claim.job_id
            return json.dumps(payload)

        worker = ReconciliationWorker(
            repo,
            rewrite_candidate,
            user_id="u",
            agent_id="a",
            mode=ReconciliationMode.ENFORCE,
            allowed_actions={ReconciliationAction.STORE},
        )

        result = await worker.run_once()

        assert result.status == "retry"
        job = await (await conn.execute("SELECT * FROM memory_reconciliation_jobs")).fetchone()
        assert job["status"] == "retry"
        assert job["retry_count"] == 1
        assert job["error"] == "action_apply_failed"
        assert job["lease_token"] is None
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_reconciliation_actions")).fetchone())[0] == 0
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_index_outbox")).fetchone())[0] == 0
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_retrieval_epochs")).fetchone())[0] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_provider_failure_is_not_masked_when_fail_job_also_fails(caplog) -> None:
    repo = _repo()
    original = RuntimeError("provider unavailable")
    retry_failure = RuntimeError("database unavailable")
    provider = AsyncMock(side_effect=original)
    repo.fail_job.side_effect = retry_failure
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    with caplog.at_level(logging.ERROR, logger="memory.reconciliation_worker"):
        with pytest.raises(RuntimeError, match="provider unavailable") as exc_info:
            await worker.run_once()

    assert exc_info.value is original
    assert exc_info.value.__cause__ is retry_failure
    assert "memory.reconciliation_worker.fail_job_failed" in caplog.text
    assert "provider unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_release_failure_does_not_mask_cancellation(caplog) -> None:
    repo = _repo()
    provider = AsyncMock(side_effect=asyncio.CancelledError)
    repo.release_lease.side_effect = RuntimeError("database unavailable")
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    with caplog.at_level(logging.ERROR, logger="memory.reconciliation_worker"):
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once()

    repo.release_lease.assert_awaited_once_with(7, "lease-7")
    assert "memory.reconciliation_worker.release_lease_failed" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_releases_lease_and_propagates() -> None:
    repo = _repo()
    provider = AsyncMock(side_effect=asyncio.CancelledError)
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()

    repo.release_lease.assert_awaited_once_with(7, "lease-7")
    repo.fail_job.assert_not_awaited()
    repo.apply_action.assert_not_awaited()


# ============================================================
# run_forever 轮询循环（P1 悬空接线）
# ============================================================

def _claim_provider_seq(claims):
    """claim_pending 依次返回 claims，耗尽后恒返 None（idle）。"""
    it = iter(claims)

    async def _claim(**kwargs):
        return next(it, None)

    return _claim


@pytest.mark.asyncio
async def test_run_forever_drains_jobs_until_idle_then_cancels_cleanly() -> None:
    repo = _repo()
    repo.claim_pending.side_effect = _claim_provider_seq([_claim(), _claim()])
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    task = asyncio.create_task(run_forever(worker, interval=0.01))
    for _ in range(500):
        if repo.record_shadow.await_count >= 2:
            break
        await asyncio.sleep(0.01)
    assert repo.record_shadow.await_count == 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_run_forever_backs_off_after_unexpected_error_and_recovers() -> None:
    repo = _repo()
    calls = {"n": 0}

    async def _flaky_claim(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient database outage")
        return None

    repo.claim_pending.side_effect = _flaky_claim
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    task = asyncio.create_task(run_forever(worker, interval=0.01))
    try:
        for _ in range(500):
            if calls["n"] >= 3:
                break
            await asyncio.sleep(0.01)
        assert calls["n"] >= 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_run_forever_cancel_during_idle_sleep_is_prompt() -> None:
    repo = _repo()
    repo.claim_pending.return_value = None
    provider = AsyncMock(return_value=_json())
    worker = ReconciliationWorker(repo, provider, user_id="u", agent_id="a")

    task = asyncio.create_task(run_forever(worker, interval=30.0))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert time.monotonic() - started < 2.0
