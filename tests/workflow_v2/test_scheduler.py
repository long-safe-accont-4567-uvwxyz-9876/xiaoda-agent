# tests/workflow_v2/test_scheduler.py
import pytest, time, aiosqlite
from workflow_v2.models import (
    NodeSpec, EdgeSpec, NodeType, StepStatus, RunStatus,
    WorkflowRevision, WorkflowRun, WorkflowStepRun, WorkflowRunEvent, Idempotency,
)
from db.db_workflow import create_schema
from workflow_v2.repository import WorkflowRepository
from workflow_v2.scheduler import Scheduler, NodeResult, compute_ready


def _rev():
    nodes = [
        NodeSpec(id="start", type=NodeType.START, name="s"),
        NodeSpec(id="a", type=NodeType.TOOL, name="a", config={"tool_ref": "t"}),
        NodeSpec(id="end", type=NodeType.END, name="e"),
    ]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    return WorkflowRevision(revision_id="rev1", workflow_id="w1", nodes=nodes, edges=edges)


def test_compute_ready_returns_start_first():
    rev = _rev()
    steps = []
    ready = compute_ready(rev, steps)
    assert [n.id for n in ready] == ["start"]


@pytest.mark.asyncio
async def test_run_completes_with_fake_executor():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    rev = _rev()

    async def fake_exec(node, step, ctx):
        return NodeResult(status=StepStatus.SUCCEEDED, output={"node": node.id})

    sched = Scheduler(repo, fake_exec, revision_provider=lambda rid: rev)
    run = WorkflowRun(run_id="r1", workflow_id="w1", revision_id="rev1", created_at=time.time())
    await repo.create_run(run, [], WorkflowRunEvent(run_id="r1", seq=1, event_type="run_queued", run_status=RunStatus.QUEUED))
    status = RunStatus.QUEUED
    for _ in range(10):
        status = await sched.tick("r1")
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            break
    assert status == RunStatus.SUCCEEDED
    await conn.close()


@pytest.mark.asyncio
async def test_recover_fails_leftover_running_non_idempotent():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    rev = _rev()
    run = WorkflowRun(run_id="r1", workflow_id="w1", revision_id="rev1", status=RunStatus.RUNNING, created_at=time.time())
    stuck = WorkflowStepRun(run_id="r1", node_id="a", attempt=1, status=StepStatus.RUNNING)
    await repo.create_run(run, [stuck], WorkflowRunEvent(run_id="r1", seq=1, event_type="run_started", run_status=RunStatus.RUNNING))

    async def never(node, step, ctx):  # should not be called during recovery
        raise AssertionError("executor must not run during recovery")

    sched = Scheduler(repo, never, revision_provider=lambda rid: rev)
    await sched.recover("r1")
    steps = await repo.events_after("r1", 0)
    assert any(e.event_type == "step_failed" and e.step_id == "a" for e in steps)
