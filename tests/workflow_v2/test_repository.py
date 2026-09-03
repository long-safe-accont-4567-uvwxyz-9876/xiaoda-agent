import asyncio
import time

import aiosqlite
import pytest

from db.db_workflow import create_schema
from workflow_v2.models import (
    RunStatus,
    StepStatus,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowStepRun,
)
from workflow_v2.repository import WorkflowRepository


@pytest.fixture
async def repo():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    yield WorkflowRepository(conn)
    await conn.close()


def _run():
    return WorkflowRun(run_id="r1", workflow_id="w1", revision_id="rev1",
                       status=RunStatus.QUEUED, lock_version=0, created_at=time.time())


@pytest.mark.asyncio
async def test_create_and_get_run(repo):
    run = _run()
    step = WorkflowStepRun(run_id="r1", node_id="start", attempt=1, status=StepStatus.PENDING)
    ev = WorkflowRunEvent(run_id="r1", seq=1, event_type="run_queued", run_status=RunStatus.QUEUED)
    await repo.create_run(run, [step], ev)
    got = await repo.get_run("r1")
    assert got.status == RunStatus.QUEUED
    assert (await repo.next_seq("r1")) == 2


@pytest.mark.asyncio
async def test_event_seq_unique(repo):
    await repo.create_run(_run(), [], WorkflowRunEvent(run_id="r1", seq=1, event_type="run_queued", run_status=RunStatus.QUEUED))
    with pytest.raises(Exception):
        await repo.append_event(WorkflowRunEvent(run_id="r1", seq=1, event_type="dup", run_status=RunStatus.QUEUED))


@pytest.mark.asyncio
async def test_commit_step_result_cas_conflict(repo):
    run = _run()
    step = WorkflowStepRun(run_id="r1", node_id="a", attempt=1, status=StepStatus.RUNNING)
    await repo.create_run(run, [step], WorkflowRunEvent(run_id="r1", seq=1, event_type="run_started", run_status=RunStatus.RUNNING))
    ev = WorkflowRunEvent(run_id="r1", seq=2, event_type="step_succeeded", run_status=RunStatus.RUNNING, step_id="a", attempt=1)
    # wrong expected_lock -> conflict
    ok = await repo.commit_step_result("r1", "a", 1, StepStatus.SUCCEEDED, {"output": {}}, RunStatus.RUNNING, expected_lock=99, event=ev)
    assert ok is False


@pytest.mark.asyncio
async def test_claim_step_with_event_is_atomic(repo):
    """Finding 1: claim + step_started event must land in ONE transaction."""
    run = _run()
    await repo.create_run(run, [], WorkflowRunEvent(run_id="r1", seq=1, event_type="run_started", run_status=RunStatus.RUNNING))
    step = await repo.claim_step_with_event(
        "r1", "a", expected_lock=0, lease_owner="worker", lease_ttl=60.0,
        event=WorkflowRunEvent(run_id="r1", seq=0, event_type="step_started",
                               run_status=RunStatus.RUNNING, step_id="a", attempt=1,
                               timestamp=time.time()),
    )
    assert step is not None
    assert step.status == StepStatus.RUNNING
    assert step.attempt == 1
    assert step.lease_owner == "worker"
    got = await repo.get_run("r1")
    assert got.status == RunStatus.RUNNING
    assert got.lock_version == 1
    steps = await repo.list_steps("r1")
    assert len(steps) == 1 and steps[0].node_id == "a" and steps[0].status == StepStatus.RUNNING
    events = await repo.events_after("r1", 0)
    started = [e for e in events if e.event_type == "step_started"]
    assert len(started) == 1
    assert started[0].step_id == "a" and started[0].attempt == 1
    assert started[0].seq == 2  # repo assigned the real next seq inside the txn


@pytest.mark.asyncio
async def test_claim_step_with_event_lock_conflict_inserts_nothing(repo):
    """Finding 1: on CAS conflict nothing (step row nor event) may be written."""
    run = _run()
    await repo.create_run(run, [], WorkflowRunEvent(run_id="r1", seq=1, event_type="run_started", run_status=RunStatus.RUNNING))
    step = await repo.claim_step_with_event(
        "r1", "a", expected_lock=99, lease_owner="worker", lease_ttl=60.0,
        event=WorkflowRunEvent(run_id="r1", seq=0, event_type="step_started",
                               run_status=RunStatus.RUNNING, step_id="a", attempt=1,
                               timestamp=time.time()),
    )
    assert step is None
    assert await repo.list_steps("r1") == []
    assert await repo.next_seq("r1") == 2  # no step_started event appended
    assert not any(e.event_type == "step_started" for e in await repo.events_after("r1", 0))


@pytest.mark.asyncio
async def test_single_connection_transactions_are_serialized(repo, monkeypatch):
    """A second repository transaction waits instead of issuing a nested BEGIN."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original_insert_step = repo._insert_step

    async def paused_insert(step):
        await original_insert_step(step)
        if step.run_id == "r1":
            entered.set()
            await release.wait()

    monkeypatch.setattr(repo, "_insert_step", paused_insert)
    first = asyncio.create_task(repo.create_run(
        _run(),
        [WorkflowStepRun(run_id="r1", node_id="a", attempt=1)],
        WorkflowRunEvent(run_id="r1", seq=1, event_type="run_queued",
                         run_status=RunStatus.QUEUED),
    ))
    await entered.wait()

    second_run = WorkflowRun(
        run_id="r2", workflow_id="w1", revision_id="rev1",
        status=RunStatus.QUEUED, created_at=time.time(),
    )
    second = asyncio.create_task(repo.create_run(
        second_run, [],
        WorkflowRunEvent(run_id="r2", seq=1, event_type="run_queued",
                         run_status=RunStatus.QUEUED),
    ))
    await asyncio.sleep(0.05)
    second_waited = not second.done()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert second_waited
    assert not [result for result in results if isinstance(result, BaseException)]
    assert await repo.get_run("r2") is not None


@pytest.mark.asyncio
async def test_cancel_fences_stale_step_result_and_increments_lock(repo):
    run = WorkflowRun(
        run_id="r1", workflow_id="w1", revision_id="rev1",
        status=RunStatus.RUNNING, lock_version=3, created_at=time.time(),
    )
    step = WorkflowStepRun(
        run_id="r1", node_id="a", attempt=1, status=StepStatus.RUNNING,
    )
    await repo.create_run(
        run, [step],
        WorkflowRunEvent(run_id="r1", seq=1, event_type="step_started",
                         run_status=RunStatus.RUNNING, step_id="a", attempt=1),
    )

    assert await repo.cancel_run("r1") is True
    cancelled = await repo.get_run("r1")
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.lock_version == 4

    committed = await repo.commit_step_result(
        "r1", "a", 1, StepStatus.SUCCEEDED, {"output": {"late": True}},
        RunStatus.RUNNING, expected_lock=3,
        event=WorkflowRunEvent(
            run_id="r1", seq=3, event_type="step_succeeded",
            run_status=RunStatus.RUNNING, step_id="a", attempt=1,
        ),
    )
    assert committed is False
    assert (await repo.get_run("r1")).status == RunStatus.CANCELLED
    assert (await repo.list_steps("r1"))[0].status == StepStatus.CANCELLED
    assert [event.event_type for event in await repo.events_after("r1", 0)] == [
        "step_started", "run_cancelled",
    ]


@pytest.mark.asyncio
async def test_waiting_step_event_and_review_commit_atomically(repo):
    run = WorkflowRun(
        run_id="r1", workflow_id="w1", revision_id="rev1",
        status=RunStatus.RUNNING, created_at=time.time(),
    )
    step = WorkflowStepRun(
        run_id="r1", node_id="review", attempt=1, status=StepStatus.RUNNING,
    )
    await repo.create_run(
        run, [step],
        WorkflowRunEvent(run_id="r1", seq=1, event_type="step_started",
                         run_status=RunStatus.RUNNING, step_id="review", attempt=1),
    )

    committed = await repo.commit_step_result(
        "r1", "review", 1, StepStatus.WAITING_INPUT, {"output": {}},
        RunStatus.RUNNING, expected_lock=0,
        event=WorkflowRunEvent(
            run_id="r1", seq=0, event_type="step_waiting_input",
            run_status=RunStatus.RUNNING, step_id="review", attempt=1,
        ),
        review={"title": "Approve?", "note": "Check output"},
    )

    assert committed is True
    assert (await repo.list_steps("r1"))[0].status == StepStatus.WAITING_INPUT
    assert [event.event_type for event in await repo.events_after("r1", 0)] == [
        "step_started", "step_waiting_input",
    ]
    reviews = await repo.list_reviews("r1")
    assert len(reviews) == 1
    assert reviews[0]["title"] == "Approve?"


@pytest.mark.asyncio
async def test_waiting_transition_rolls_back_when_review_insert_crashes(
        repo, monkeypatch):
    run = WorkflowRun(
        run_id="r1", workflow_id="w1", revision_id="rev1",
        status=RunStatus.RUNNING, created_at=time.time(),
    )
    step = WorkflowStepRun(
        run_id="r1", node_id="review", attempt=1, status=StepStatus.RUNNING,
    )
    await repo.create_run(
        run, [step],
        WorkflowRunEvent(run_id="r1", seq=1, event_type="step_started",
                         run_status=RunStatus.RUNNING, step_id="review", attempt=1),
    )

    async def crash(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(repo, "_insert_review", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await repo.commit_step_result(
            "r1", "review", 1, StepStatus.WAITING_INPUT, {"output": {}},
            RunStatus.RUNNING, expected_lock=0,
            event=WorkflowRunEvent(
                run_id="r1", seq=0, event_type="step_waiting_input",
                run_status=RunStatus.RUNNING, step_id="review", attempt=1,
            ),
            review={"title": "Approve?", "note": ""},
        )

    unchanged = await repo.get_run("r1")
    assert unchanged.status == RunStatus.RUNNING
    assert unchanged.lock_version == 0
    assert (await repo.list_steps("r1"))[0].status == StepStatus.RUNNING
    assert len(await repo.events_after("r1", 0)) == 1
    assert await repo.list_reviews("r1") == []


@pytest.mark.asyncio
async def test_create_review_returns_canonical_id_for_duplicate_attempt(repo):
    first = await repo.create_review(
        "r1", "review", 1, title="First", note="", review_id="rev-first",
    )
    second = await repo.create_review(
        "r1", "review", 1, title="Second", note="", review_id="rev-second",
    )

    assert first == "rev-first"
    assert second == first
    reviews = await repo.list_reviews("r1")
    assert len(reviews) == 1
    assert reviews[0]["review_id"] == first
