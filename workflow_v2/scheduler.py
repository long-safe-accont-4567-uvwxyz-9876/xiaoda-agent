# workflow_v2/scheduler.py
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from workflow_v2.models import (
    NodeSpec, WorkflowRevision, WorkflowStepRun, WorkflowRunEvent,
    RunStatus, StepStatus, NodeType, FailurePolicy,
)
from workflow_v2.repository import WorkflowRepository


@dataclass
class NodeResult:
    status: StepStatus
    output: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


ExecutorFn = Callable[[NodeSpec, WorkflowStepRun, dict], Awaitable[NodeResult]]
RevisionProvider = Callable[[str], Awaitable[WorkflowRevision]]


def compute_ready(revision: WorkflowRevision, steps: list[WorkflowStepRun]) -> list[NodeSpec]:
    by_id = {n.id: n for n in revision.nodes}
    incoming: dict[str, list[str]] = {n.id: [] for n in revision.nodes}
    for e in revision.edges:
        incoming[e.target].append(e.source)
    terminal = {(s.node_id, s.status) for s in steps}
    done = {s.node_id for s in steps if s.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)}
    started = {s.node_id for s in steps}
    ready = []
    for n in revision.nodes:
        if n.id in started:
            continue
        preds = incoming[n.id]
        if all(p in done for p in preds):
            ready.append(n)
    return ready


class Scheduler:
    def __init__(self, repo: WorkflowRepository, executor: ExecutorFn,
                 revision_provider: RevisionProvider, lease_ttl: float = 60.0,
                 metrics: Any = None):
        self.repo = repo
        self.executor = executor
        self.revision_provider = revision_provider
        self.lease_ttl = lease_ttl
        # M4 观测：非空时按节点结果记 step 计数（不影响任何状态判定）
        self.metrics = metrics

    async def tick(self, run_id: str) -> RunStatus:
        run = await self.repo.get_run(run_id)
        if run is None or run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            return run.status if run else RunStatus.FAILED
        rev = await self.revision_provider(run.revision_id)
        if rev is None:
            # 数据不一致（如 revision 被清理）：跳过本轮，等写侧修复，不炸驱动
            logger.warning("workflow.tick_revision_missing run_id={} revision={}",
                           run_id, run.revision_id)
            return run.status
        steps = await self._steps(run_id)
        ready = compute_ready(rev, steps)
        if not ready:
            if self._all_ends_done(rev, steps):
                await self._finish(run_id, RunStatus.SUCCEEDED)
                return RunStatus.SUCCEEDED
            return run.status
        for node in ready:
            run = await self.repo.get_run(run_id)
            if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
                break  # terminal run: no new work (claim would regress status)
            claimed = await self.repo.claim_step_with_event(
                run_id, node.id, run.lock_version, "worker", self.lease_ttl,
                WorkflowRunEvent(run_id=run_id, seq=0, event_type="step_started",
                                 run_status=RunStatus.RUNNING, step_id=node.id,
                                 attempt=1, timestamp=time.time()),
            )
            if claimed is None:
                continue
            result = await self.executor(node, claimed, {"run": run.input})
            await self._commit(run_id, node, claimed.attempt, result)
            if result.status == StepStatus.WAITING_INPUT:
                # M4 REVIEW：执行器只声明"等人"，审批单落库后 DAG 停在 WAITING
                # 步骤（compute_ready 不会重取已启动节点），决策端点批准/拒绝
                # 才恢复推进或停流。
                await self._require_review(run_id, node, claimed.attempt, result)
        return (await self.repo.get_run(run_id)).status

    async def _require_review(self, run_id: str, node: NodeSpec,
                              attempt: int, result: NodeResult) -> None:
        """为 WAITING 的 REVIEW 步骤建审批单（幂等——同一步骤只落一张）。"""
        cfg = node.config or {}
        wf_id = None
        run = await self.repo.get_run(run_id)
        if run is not None:
            wf_id = run.workflow_id
        await self.repo.create_review(
            run_id, node.id, attempt,
            title=str(cfg.get("title") or node.name or "人工审批"),
            note=str(cfg.get("note") or ""))
        if self.metrics is not None and wf_id is not None:
            self.metrics.review_created(wf_id)

    def _all_ends_done(self, rev: WorkflowRevision, steps: list[WorkflowStepRun]) -> bool:
        ends = [n.id for n in rev.nodes if n.type == NodeType.END]
        done = {s.node_id for s in steps if s.status == StepStatus.SUCCEEDED}
        return bool(ends) and any(e in done for e in ends)

    async def _commit(self, run_id: str, node: NodeSpec, attempt: int, result: NodeResult) -> None:
        run = await self.repo.get_run(run_id)
        # Terminal-state guard: never regress an already-terminal run (e.g. a
        # FAILED run must stay FAILED even if another diamond branch later
        # succeeds) — otherwise the run never terminates.
        run_status = (run.status if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)
                      else (RunStatus.FAILED if result.status == StepStatus.FAILED and node.failure_policy == FailurePolicy.FAIL_RUN
                            else RunStatus.RUNNING))
        event_type = f"step_{result.status.value}"
        committed = await self.repo.commit_step_result(
            run_id, node.id, attempt, result.status,
            {"output": result.output, "error_code": result.error_code, "error_message": result.error_message},
            run_status, run.lock_version,
            WorkflowRunEvent(run_id=run_id, seq=await self.repo.next_seq(run_id),
                             event_type=event_type, run_status=run_status,
                             step_id=node.id, attempt=attempt, timestamp=time.time(),
                             payload=result.output),
        )
        # M4 观测：只统计真正落库的步骤结果（CAS 失败/终态守卫不计）
        if committed and self.metrics is not None:
            self.metrics.step_finished(run.workflow_id,
                                       result.status == StepStatus.SUCCEEDED)

    async def _finish(self, run_id: str, status: RunStatus) -> None:
        run = await self.repo.get_run(run_id)
        await self.repo.commit_step_result(
            run_id, "__run__", 0, StepStatus.SUCCEEDED, {"output": {}}, status, run.lock_version,
            WorkflowRunEvent(run_id=run_id, seq=await self.repo.next_seq(run_id),
                             event_type=f"run_{status.value}", run_status=status, timestamp=time.time()))

    async def recover(self, run_id: str) -> None:
        rev = await self.revision_provider((await self.repo.get_run(run_id)).revision_id)
        if rev is None:
            logger.warning("workflow.recover_revision_missing run_id={}", run_id)
            return
        by_id = {n.id: n for n in rev.nodes}
        steps = await self._steps(run_id)
        for s in steps:
            if s.status != StepStatus.RUNNING:
                continue
            node = by_id.get(s.node_id)
            if node and node.idempotency.mode == "required":
                # Conservative recovery: idempotent leftover-running nodes stay
                # eligible to resume — reset the stuck step back to PENDING
                # (attempt row stays) and record a step_retry_scheduled event so
                # compute_ready re-picks it on the next tick; the run stays
                # RUNNING. waiting_input nodes are left untouched (deferred:
                # they resume when the input arrives).
                run = await self.repo.get_run(run_id)
                await self.repo.commit_step_result(
                    run_id, s.node_id, s.attempt, StepStatus.PENDING,
                    {"output": {}, "error_code": None, "error_message": None},
                    RunStatus.RUNNING, run.lock_version,
                    WorkflowRunEvent(run_id=run_id, seq=await self.repo.next_seq(run_id),
                                     event_type="step_retry_scheduled", run_status=RunStatus.RUNNING,
                                     step_id=s.node_id, attempt=s.attempt, timestamp=time.time(),
                                     payload={"reason": "recovered after restart"}))
                continue
            run = await self.repo.get_run(run_id)
            await self.repo.commit_step_result(
                run_id, s.node_id, s.attempt, StepStatus.FAILED,
                {"output": {}, "error_code": "EXECUTION_STATE_UNKNOWN",
                 "error_message": "process restarted while node was running"},
                RunStatus.FAILED, run.lock_version,
                WorkflowRunEvent(run_id=run_id, seq=await self.repo.next_seq(run_id),
                                 event_type="step_failed", run_status=RunStatus.FAILED,
                                 step_id=s.node_id, attempt=s.attempt, timestamp=time.time(),
                                 payload={"error_code": "EXECUTION_STATE_UNKNOWN"}))

    async def _steps(self, run_id: str) -> list[WorkflowStepRun]:
        cur = await self.repo.conn.execute(
            "SELECT * FROM wf_step_run WHERE run_id=? ORDER BY attempt", (run_id,))
        rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append(WorkflowStepRun(
                run_id=r["run_id"], node_id=r["node_id"], attempt=r["attempt"],
                status=StepStatus(r["status"]),
                error_code=r["error_code"], error_message=r["error_message"],
                lease_owner=r["lease_owner"], lease_expires_at=r["lease_expires_at"]))
        return out
