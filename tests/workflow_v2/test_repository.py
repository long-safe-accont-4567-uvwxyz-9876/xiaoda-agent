import time
import pytest
import aiosqlite
from workflow_v2.models import (
    WorkflowRun, WorkflowStepRun, WorkflowRunEvent, RunStatus, StepStatus,
)
from db.db_workflow import create_schema
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
