# tests/workflow_v2/test_scheduler.py
import time

import aiosqlite
import pytest

from db.db_workflow import create_schema
from workflow_v2.models import (
    EdgeSpec,
    Idempotency,
    NodeSpec,
    NodeType,
    RunStatus,
    StepStatus,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowStepRun,
)
from workflow_v2.repository import WorkflowRepository
from workflow_v2.scheduler import NodeResult, Scheduler, compute_ready


def _rev():
    nodes = [
        NodeSpec(id="start", type=NodeType.START, name="s"),
        NodeSpec(id="a", type=NodeType.TOOL, name="a", config={"tool_ref": "t"}),
        NodeSpec(id="end", type=NodeType.END, name="e"),
    ]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    return WorkflowRevision(revision_id="rev1", workflow_id="w1", nodes=nodes, edges=edges)


def _rev_provider(rev):
    """RevisionProvider 契约是可等待回调：直接透传固定 revision。"""
    async def provider(revision_id: str):
        return rev
    return provider


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

    sched = Scheduler(repo, fake_exec, revision_provider=_rev_provider(rev))
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

    sched = Scheduler(repo, never, revision_provider=_rev_provider(rev))
    await sched.recover("r1")
    steps = await repo.events_after("r1", 0)
    assert any(e.event_type == "step_failed" and e.step_id == "a" for e in steps)
    await conn.close()


@pytest.mark.asyncio
async def test_recover_resets_leftover_running_idempotent_node():
    """Finding 2: idempotent leftover-RUNNING step must be reset to PENDING with a
    step_retry_scheduled event so compute_ready re-picks it; run must not be failed."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    nodes = [
        NodeSpec(id="start", type=NodeType.START, name="s"),
        NodeSpec(id="a", type=NodeType.TOOL, name="a", config={"tool_ref": "t"},
                 idempotency=Idempotency(mode="required")),
        NodeSpec(id="end", type=NodeType.END, name="e"),
    ]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    rev = WorkflowRevision(revision_id="rev1", workflow_id="w1", nodes=nodes, edges=edges)
    run = WorkflowRun(run_id="r1", workflow_id="w1", revision_id="rev1",
                      status=RunStatus.RUNNING, created_at=time.time())
    stuck = WorkflowStepRun(run_id="r1", node_id="a", attempt=1, status=StepStatus.RUNNING,
                            lease_owner="dead-worker")
    await repo.create_run(run, [stuck], WorkflowRunEvent(run_id="r1", seq=1,
                          event_type="run_started", run_status=RunStatus.RUNNING))

    async def never(node, step, ctx):  # should not be called during recovery
        raise AssertionError("executor must not run during recovery")

    sched = Scheduler(repo, never, revision_provider=_rev_provider(rev))
    await sched.recover("r1")
    steps = await repo.list_steps("r1")
    a_step = next(s for s in steps if s.node_id == "a")
    assert a_step.status == StepStatus.PENDING
    assert a_step.attempt == 1  # attempt row stays; the step is re-runnable
    events = await repo.events_after("r1", 0)
    assert any(e.event_type == "step_retry_scheduled" and e.step_id == "a" and e.attempt == 1
               for e in events)
    run_after = await repo.get_run("r1")
    assert run_after.status != RunStatus.FAILED
    await conn.close()


@pytest.mark.asyncio
async def test_diamond_failed_branch_keeps_run_failed():
    """Finding 3: a FAILED branch must never be overwritten back to RUNNING by a
    concurrent succeeding branch (_commit terminal-state guard)."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    nodes = [
        NodeSpec(id="start", type=NodeType.START, name="s"),
        NodeSpec(id="a", type=NodeType.TOOL, name="a", config={"tool_ref": "t"}),
        NodeSpec(id="b", type=NodeType.TOOL, name="b", config={"tool_ref": "t"}),
        NodeSpec(id="end", type=NodeType.END, name="e"),
    ]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="start", target="b"),
             EdgeSpec(source="a", target="end"), EdgeSpec(source="b", target="end")]
    rev = WorkflowRevision(revision_id="rev1", workflow_id="w1", nodes=nodes, edges=edges)

    async def fake_exec(node, step, ctx):
        if node.id == "a":
            return NodeResult(status=StepStatus.FAILED, error_code="ERR", error_message="boom")
        return NodeResult(status=StepStatus.SUCCEEDED, output={"node": node.id})

    sched = Scheduler(repo, fake_exec, revision_provider=_rev_provider(rev))
    run = WorkflowRun(run_id="r1", workflow_id="w1", revision_id="rev1", created_at=time.time())
    await repo.create_run(run, [], WorkflowRunEvent(run_id="r1", seq=1,
                          event_type="run_queued", run_status=RunStatus.QUEUED))
    status = RunStatus.QUEUED
    for _ in range(10):
        status = await sched.tick("r1")
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            break
    assert status == RunStatus.FAILED
    assert (await repo.get_run("r1")).status == RunStatus.FAILED
    await conn.close()
