"""M4 加固/可观测：REVIEW 高级节点 + 负载节流 + workflow 级指标。

覆盖（立项书 M4）：
- REVIEW/APPROVAL 节点：执行器置 WAITING → 审批单落库 → 批准续跑 / 拒绝
  停流（REVIEW_REJECTED）；重复决策/终态 run → 冲突不重复计分；
- 负载节流：max_concurrent=1 时第二个 QUEUED run 不被启动，空出名额自动续跑；
- 观测：WorkflowMetrics 按 wf_id 计数（运行/步骤/审批 + debug 计数）。

REVIEW 流程用真实 UnifiedExecutor；节流用挂起执行器 + 驱动轮询。
"""
from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from db.db_workflow import create_schema
from workflow_v2.app import WorkflowDriver
from workflow_v2.executor import ExecutorServices, UnifiedExecutor
from workflow_v2.metrics import WorkflowMetrics
from workflow_v2.models import (
    EdgeSpec,
    NodeSpec,
    NodeType,
    RunStatus,
    StepStatus,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunEvent,
)
from workflow_v2.repository import WorkflowRepository
from workflow_v2.scheduler import NodeResult, Scheduler


def _graph(workflow_id: str = "wf-m4", revision_id: str = "rev-m4",
           node_type: NodeType = NodeType.REVIEW) -> WorkflowRevision:
    """start → (REVIEW/APPROVAL) → end 用例图。"""
    return WorkflowRevision(
        revision_id=revision_id, workflow_id=workflow_id, nodes=[
            NodeSpec(id="start", type=NodeType.START, name="start"),
            NodeSpec(id=node_type.value, type=node_type, name="闸门",
                     config={"title": "审批人？", "note": "确认后放行"}),
            NodeSpec(id="end", type=NodeType.END, name="end"),
        ],
        edges=[EdgeSpec(source="start", target=node_type.value),
               EdgeSpec(source=node_type.value, target="end")])


async def _make_runtime(workflow_id: str = "wf-m4", run_id: str = "r-m4",
                        rev: WorkflowRevision | None = None) -> tuple[WorkflowRepository, aiosqlite.Connection]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    graph = rev or _graph(workflow_id)
    await repo.upsert_definition(workflow_id=workflow_id, name="m4")
    await repo.insert_revision(graph)
    await repo.set_current_revision(workflow_id, graph.revision_id)
    run = WorkflowRun(run_id=run_id, workflow_id=workflow_id,
                      revision_id=graph.revision_id, created_at=time.time())
    await repo.create_run(run, [], WorkflowRunEvent(
        run_id=run_id, seq=1, event_type="run_queued",
        run_status=RunStatus.QUEUED, timestamp=time.time()))
    return repo, conn


# 真实执行器：REVIEW 步骤返回 WAITING_INPUT（不触达任何能力层）
def _real_exec() -> UnifiedExecutor:
    return UnifiedExecutor(ExecutorServices())


class _OkExecutor:
    """任意节点直接成功（只用于无 REVIEW 节点的普通图）。"""

    async def __call__(self, node, step, ctx):
        return NodeResult(status=StepStatus.SUCCEEDED, output={"node": node.id})


async def _run_to_success(repo: WorkflowRepository, scheduler: Scheduler, run_id: str):
    for _ in range(10):
        await scheduler.tick(run_id)
        if (await repo.get_run(run_id)).status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
            break
    return (await repo.get_run(run_id)).status


# ---------------------------------------------------------------------------
# REVIEW 节点：暂停 / 批准续跑 / 拒绝停流 / 重复决策
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_node_pauses_and_stays_single():
    repo, conn = await _make_runtime()
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    try:
        for _ in range(3):
            await scheduler.tick("r-m4")
        run = await repo.get_run("r-m4")
        assert run.status == RunStatus.RUNNING
        steps = await repo.list_steps("r-m4")
        mid = next(s for s in steps if s.node_id == "review")
        assert mid.status == StepStatus.WAITING_INPUT
        # 反复 tick 不重复建审批单、不推进下游
        reviews = await repo.list_reviews("r-m4")
        assert len(reviews) == 1 and reviews[0]["status"] == "pending"
        assert reviews[0]["title"] == "审批人？"
        assert len([s for s in steps if s.node_id == "review"]) == 1
        assert not any(s.node_id == "end" for s in steps)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_review_approval_resumes_to_end():
    repo, conn = await _make_runtime()
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    try:
        await scheduler.tick("r-m4")  # start
        await scheduler.tick("r-m4")  # review → WAITING + 审批单
        rid = (await repo.list_reviews("r-m4"))[0]["review_id"]
        out = await repo.resolve_review(rid, "approve", "tester", "OK")
        assert out is not None
        status = await _run_to_success(repo, scheduler, "r-m4")
        assert status == RunStatus.SUCCEEDED
        step = next(s for s in await repo.list_steps("r-m4") if s.node_id == "review")
        assert step.status == StepStatus.SUCCEEDED
        assert (await repo.list_reviews("r-m4"))[0]["status"] == "approved"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_review_reject_fails_run_and_no_side_effects():
    repo, conn = await _make_runtime()
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    try:
        await scheduler.tick("r-m4")  # start
        await scheduler.tick("r-m4")  # review
        rid = (await repo.list_reviews("r-m4"))[0]["review_id"]
        assert await repo.resolve_review(rid, "reject", "tester", "内容不合规") is not None
        run = await repo.get_run("r-m4")
        assert run.status == RunStatus.FAILED
        step = next(s for s in await repo.list_steps("r-m4") if s.node_id == "review")
        assert step.status == StepStatus.FAILED
        assert step.error_code == "REVIEW_REJECTED"
        # 终态后 tick 无副作用（不会复活）
        await scheduler.tick("r-m4")
        assert (await repo.get_run("r-m4")).status == RunStatus.FAILED
        assert (await repo.list_reviews("r-m4"))[0]["status"] == "rejected"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_review_double_decision_conflict_and_missing():
    repo, conn = await _make_runtime()
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    try:
        await scheduler.tick("r-m4")  # start
        await scheduler.tick("r-m4")  # review
        rid = (await repo.list_reviews("r-m4"))[0]["review_id"]
        assert await repo.resolve_review(rid, "approve", "u1") is not None
        # 重复决策（同单或先拒后批）→ None，不覆盖首次结果、不再改 run
        assert await repo.resolve_review(rid, "reject", "u2") is None
        reviews = await repo.list_reviews("r-m4")
        assert reviews[0]["status"] == "approved"
        assert reviews[0]["decided_by"] == "u1"
        # 不存在的审批单 → None
        assert await repo.resolve_review("rev-nope", "approve", "u1") is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_approval_node_type_alias_same_semantics():
    """APPROVAL 类型节点（老图沿用）与 REVIEW 同语义。"""
    repo, conn = await _make_runtime(
        workflow_id="wf-ap", run_id="r-ap",
        rev=_graph("wf-ap", "rev-ap", node_type=NodeType.APPROVAL))
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    try:
        await scheduler.tick("r-ap")  # start
        await scheduler.tick("r-ap")  # approval
        reviews = await repo.list_reviews("r-ap")
        assert len(reviews) == 1 and reviews[0]["node_id"] == "approval"
        await repo.resolve_review(reviews[0]["review_id"], "approve", "tester")
        status = await _run_to_success(repo, scheduler, "r-ap")
        assert status == RunStatus.SUCCEEDED
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 驱动层：waiting 轮询无副作用；负载节流
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_driver_polls_waiting_review_run_without_side_effects():
    repo, conn = await _make_runtime()
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)

    async def gate(_wf: str) -> bool:
        return True

    driver = WorkflowDriver(repo, scheduler, is_enabled=gate)
    try:
        await driver._poll_once()  # start
        await driver._poll_once()  # review → waiting + 审批单
        assert (await repo.get_run("r-m4")).status == RunStatus.RUNNING
        assert len(await repo.list_reviews("r-m4")) == 1
        await driver._poll_once()
        assert len(await repo.list_reviews("r-m4")) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_driver_throttles_queued_run_until_slot_frees():
    """max_concurrent=1：run1 挂起时 run2 保持 QUEUED；释放后 run2 继续执行。"""
    repo, conn = await _make_runtime(workflow_id="wf-a", run_id="r-a")
    # 第二个工作流（普通直连图，无 REVIEW）
    graph_b = _graph("wf-b", "rev-b", node_type=NodeType.REVIEW)
    await repo.insert_revision(graph_b)
    await repo.set_current_revision("wf-b", "rev-b")
    await repo.create_run(
        WorkflowRun(run_id="r-b", workflow_id="wf-b", revision_id="rev-b",
                    created_at=time.time() + 1),
        [], WorkflowRunEvent(run_id="r-b", seq=1, event_type="run_queued",
                             run_status=RunStatus.QUEUED, timestamp=time.time()))
    # run-a 挂起执行器：claim 后停在 RUNNING
    hold = asyncio.Event()

    class _HoldExecutor:
        async def __call__(self, node, step, ctx):
            await hold.wait()
            return NodeResult(status=StepStatus.SUCCEEDED, output={"node": node.id})

    scheduler = Scheduler(repo, _HoldExecutor(), repo.get_revision)
    driver = WorkflowDriver(repo, scheduler, max_concurrent=1)
    try:
        # 让 run_a 先 claim 到 RUNNING 并挂起
        t_a = asyncio.create_task(scheduler.tick("r-a"))
        await asyncio.sleep(0.05)
        assert (await repo.get_run("r-a")).status == RunStatus.RUNNING
        # 轮询：run_b 因 running=1 ≥ max 不被启动
        await driver._poll_once()
        assert (await repo.get_run("r-b")).status == RunStatus.QUEUED
        await driver._poll_once()
        assert (await repo.get_run("r-b")).status == RunStatus.QUEUED
        # 释放 → 名额空出 → 下一轮 run_b 被执行到成功
        hold.set()
        await t_a
        status_b = await _run_to_success(repo, scheduler, "r-b")
        assert status_b == RunStatus.SUCCEEDED
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 服务层：指标快照 + 节流配置
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_snapshot_counters_and_safety():
    repo, conn = await _make_runtime()
    try:
        from workflow_v2.service import WorkflowV2Service
        svc = WorkflowV2Service(repo)
        metrics = WorkflowMetrics()
        metrics.run_started("wf-a")
        metrics.run_finished("wf-a", "succeeded")
        metrics.step_finished("wf-a", True)
        metrics.step_finished("wf-a", False)
        metrics.review_created("wf-a")
        metrics.review_decided("wf-a")
        svc.metrics = metrics
        snap = svc.metrics_snapshot()
        row = snap["workflows"]["wf-a"]
        assert row["runs_started"] == 1 and row["runs_succeeded"] == 1
        assert row["steps_succeeded"] == 1 and row["steps_failed"] == 1
        # debug 计数：reviews_pending = created - decided
        assert row["reviews_created"] == 1 and row["reviews_pending"] == 0
        assert snap["totals"]["runs_started"] == 1
        # 无 metrics 注入 → 空结构安全
        svc2 = WorkflowV2Service(repo)
        assert svc2.metrics_snapshot() == {"workflows": {}, "totals": {}}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_max_concurrent_config_roundtrip_and_fallback():
    repo, conn = await _make_runtime()
    try:
        from workflow_v2.service import WorkflowV2Service
        svc = WorkflowV2Service(repo)
        assert await svc.max_concurrent_runs() == 4  # 默认 4
        await svc.set_config("workflow_v2.max_concurrent_runs", 1)
        assert await svc.max_concurrent_runs() == 1
        await svc.set_config("workflow_v2.max_concurrent_runs", "abc")
        assert await svc.max_concurrent_runs() == 4  # 非法值回落默认
        await svc.set_config("workflow_v2.max_concurrent_runs", 0)
        assert await svc.max_concurrent_runs() == 0  # 0 = 不限制
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 路由层（conftest 的 app 脚手架 + 共享 repo）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_review_flow_approve_then_409_and_404(app, client, repo, auth_headers):
    graph = _graph("w1", "rev-x")
    await repo.upsert_definition(workflow_id="w1", name="w1")
    await repo.insert_revision(graph)
    await repo.set_current_revision("w1", "rev-x")
    await repo.create_run(
        WorkflowRun(run_id="r-x", workflow_id="w1", revision_id="rev-x",
                    created_at=time.time()),
        [], WorkflowRunEvent(run_id="r-x", seq=1, event_type="run_queued",
                             run_status=RunStatus.QUEUED, timestamp=time.time()))
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    await scheduler.tick("r-x")  # start
    await scheduler.tick("r-x")  # review → 审批单
    rid = (await repo.list_reviews("r-x"))[0]["review_id"]
    # 待批列表
    resp = await client.get("/api/v1/workflow-runs/r-x/reviews",
                            headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["status"] == "pending"
    # 非法 decision → 422
    resp = await client.post(f"/api/v1/workflow-runs/r-x/reviews/{rid}/decide",
                             json={"decision": "maybe"}, headers=auth_headers)
    assert resp.status_code == 422
    # 批准 → 放行
    resp = await client.post(f"/api/v1/workflow-runs/r-x/reviews/{rid}/decide",
                             json={"decision": "approve", "note": "过了"},
                             headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["decision"] == "approve"
    # 重复决策 → 409
    resp = await client.post(f"/api/v1/workflow-runs/r-x/reviews/{rid}/decide",
                             json={"decision": "reject"}, headers=auth_headers)
    assert resp.status_code == 409
    # 不存在 → 404
    resp = await client.post("/api/v1/workflow-runs/r-x/reviews/rev-nope/decide",
                             json={"decision": "approve"}, headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_router_reject_marks_run_failed(app, client, repo, auth_headers):
    graph = _graph("w1", "rev-y")
    await repo.upsert_definition(workflow_id="w1", name="w1")
    await repo.insert_revision(graph)
    await repo.set_current_revision("w1", "rev-y")
    await repo.create_run(
        WorkflowRun(run_id="r-y", workflow_id="w1", revision_id="rev-y",
                    created_at=time.time()),
        [], WorkflowRunEvent(run_id="r-y", seq=1, event_type="run_queued",
                             run_status=RunStatus.QUEUED, timestamp=time.time()))
    scheduler = Scheduler(repo, _real_exec(), repo.get_revision)
    await scheduler.tick("r-y")  # start
    await scheduler.tick("r-y")  # review → 审批单
    rid = (await repo.list_reviews("r-y"))[0]["review_id"]
    resp = await client.post(f"/api/v1/workflow-runs/r-y/reviews/{rid}/decide",
                             json={"decision": "reject", "note": "不行"},
                             headers=auth_headers)
    assert resp.status_code == 200
    assert (await repo.get_run("r-y")).status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_workflow_metrics_endpoint_returns_empty_shape(app, client, auth_headers):
    resp = await client.get("/api/v1/workflow-metrics", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == {"workflows", "totals"}
