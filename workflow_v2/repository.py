from __future__ import annotations
import json
import time
from uuid import uuid4

import aiosqlite

from workflow_v2.models import (
    WorkflowRun, WorkflowStepRun, WorkflowRunEvent, WorkflowRevision,
    RunStatus, StepStatus,
)


class WorkflowRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create_run(self, run: WorkflowRun, steps: list[WorkflowStepRun], first_event: WorkflowRunEvent) -> None:
        await self.conn.execute("BEGIN")
        try:
            await self.conn.execute(
                """INSERT INTO wf_run(run_id, workflow_id, revision_id, status, lock_version,
                    parent_run_id, idempotency_key, input_json, output_json, cancel_requested_at,
                    created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run.run_id, run.workflow_id, run.revision_id, run.status.value, run.lock_version,
                 run.parent_run_id, run.idempotency_key, json.dumps(run.input), json.dumps(run.output),
                 run.cancel_requested_at, run.created_at, run.updated_at),
            )
            for s in steps:
                await self._insert_step(s)
            await self._insert_event(first_event)
            await self.conn.commit()
        except (aiosqlite.Error, json.JSONDecodeError, ValueError):
            await self.conn.rollback()
            raise

    async def _insert_step(self, s: WorkflowStepRun) -> None:
        await self.conn.execute(
            """INSERT INTO wf_step_run(run_id, node_id, attempt, status, input_json, output_json,
                error_code, error_message, lease_owner, lease_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (s.run_id, s.node_id, s.attempt, s.status.value, json.dumps(s.input), json.dumps(s.output),
             s.error_code, s.error_message, s.lease_owner, s.lease_expires_at),
        )

    async def _insert_event(self, ev: WorkflowRunEvent) -> None:
        await self.conn.execute(
            """INSERT INTO wf_run_event(run_id, seq, event_type, run_status, step_id, attempt,
                payload_json, timestamp, schema_version)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (ev.run_id, ev.seq, ev.event_type, ev.run_status.value, ev.step_id, ev.attempt,
             json.dumps(ev.payload), ev.timestamp or time.time(), ev.schema_version),
        )

    async def next_seq(self, run_id: str) -> int:
        cur = await self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM wf_run_event WHERE run_id=?", (run_id,))
        row = await cur.fetchone()
        return int(row[0])

    async def append_event(self, ev: WorkflowRunEvent) -> int:
        await self._insert_event(ev)
        await self.conn.commit()
        return ev.seq

    async def claim_step(self, run_id: str, node_id: str, expected_lock: int, lease_owner: str, lease_ttl: float):
        await self.conn.execute("BEGIN")
        try:
            cur = await self.conn.execute(
                "UPDATE wf_run SET lock_version=lock_version+1, status=?, updated_at=? "
                "WHERE run_id=? AND lock_version=?",
                (RunStatus.RUNNING.value, time.time(), run_id, expected_lock),
            )
            if cur.rowcount != 1:
                await self.conn.rollback()
                return None
            cur2 = await self.conn.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM wf_step_run WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            )
            attempt = int((await cur2.fetchone())[0])
            step = WorkflowStepRun(
                run_id=run_id, node_id=node_id, attempt=attempt, status=StepStatus.RUNNING,
                lease_owner=lease_owner, lease_expires_at=time.time() + lease_ttl,
            )
            await self._insert_step(step)
            await self.conn.commit()
            return step
        except (aiosqlite.Error, ValueError):
            await self.conn.rollback()
            raise

    async def claim_step_with_event(self, run_id: str, node_id: str, expected_lock: int,
                                    lease_owner: str, lease_ttl: float,
                                    event: WorkflowRunEvent):
        """Atomically claim a step and append its starting event in ONE transaction.

        A crash between a plain claim and a separate append_event would leave the
        step RUNNING with no step_started event; doing both under the same
        BEGIN/COMMIT makes the state transition + RunEvent atomic. The event's
        seq and attempt are owned by this method (next-seq + new attempt row).
        Returns the claimed WorkflowStepRun, or None on CAS lock conflict.
        """
        await self.conn.execute("BEGIN")
        try:
            cur = await self.conn.execute(
                "UPDATE wf_run SET lock_version=lock_version+1, status=?, updated_at=? "
                "WHERE run_id=? AND lock_version=?",
                (RunStatus.RUNNING.value, time.time(), run_id, expected_lock),
            )
            if cur.rowcount != 1:
                await self.conn.rollback()
                return None
            cur2 = await self.conn.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM wf_step_run WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            )
            attempt = int((await cur2.fetchone())[0])
            step = WorkflowStepRun(
                run_id=run_id, node_id=node_id, attempt=attempt, status=StepStatus.RUNNING,
                lease_owner=lease_owner, lease_expires_at=time.time() + lease_ttl,
            )
            await self._insert_step(step)
            await self._insert_event(event.model_copy(update={
                "seq": await self.next_seq(run_id), "step_id": node_id, "attempt": attempt,
            }))
            await self.conn.commit()
            return step
        except (aiosqlite.Error, ValueError):
            await self.conn.rollback()
            raise

    async def commit_step_result(self, run_id: str, node_id: str, attempt: int,
                                 step_status: StepStatus, step_patch: dict,
                                 run_status: RunStatus, expected_lock: int,
                                 event: WorkflowRunEvent) -> bool:
        await self.conn.execute("BEGIN")
        try:
            cur = await self.conn.execute(
                "UPDATE wf_run SET status=?, lock_version=lock_version+1, updated_at=? "
                "WHERE run_id=? AND lock_version=?",
                (run_status.value, time.time(), run_id, expected_lock),
            )
            if cur.rowcount != 1:
                await self.conn.rollback()
                return False
            await self.conn.execute(
                "UPDATE wf_step_run SET status=?, output_json=?, error_code=?, error_message=? "
                "WHERE run_id=? AND node_id=? AND attempt=?",
                (step_status.value, json.dumps(step_patch.get("output", {})),
                 step_patch.get("error_code"), step_patch.get("error_message"),
                 run_id, node_id, attempt),
            )
            await self._insert_event(event)
            await self.conn.commit()
            return True
        except (aiosqlite.Error, json.JSONDecodeError, ValueError):
            await self.conn.rollback()
            raise

    async def get_run(self, run_id: str):
        cur = await self.conn.execute("SELECT * FROM wf_run WHERE run_id=?", (run_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return WorkflowRun(
            run_id=row["run_id"], workflow_id=row["workflow_id"], revision_id=row["revision_id"],
            status=RunStatus(row["status"]), lock_version=row["lock_version"],
            parent_run_id=row["parent_run_id"], idempotency_key=row["idempotency_key"],
            input=json.loads(row["input_json"]), output=json.loads(row["output_json"]),
            cancel_requested_at=row["cancel_requested_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def list_steps(self, run_id: str) -> list[WorkflowStepRun]:
        cur = await self.conn.execute(
            "SELECT * FROM wf_step_run WHERE run_id=? ORDER BY node_id, attempt", (run_id,)
        )
        rows = await cur.fetchall()
        return [
            WorkflowStepRun(
                run_id=r["run_id"], node_id=r["node_id"], attempt=r["attempt"],
                status=StepStatus(r["status"]), input=json.loads(r["input_json"]),
                output=json.loads(r["output_json"]), error_code=r["error_code"],
                error_message=r["error_message"], lease_owner=r["lease_owner"],
                lease_expires_at=r["lease_expires_at"],
            )
            for r in rows
        ]

    async def events_after(self, run_id: str, after_seq: int) -> list[WorkflowRunEvent]:
        cur = await self.conn.execute(
            "SELECT * FROM wf_run_event WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq)
        )
        rows = await cur.fetchall()
        return [
            WorkflowRunEvent(
                run_id=r["run_id"], seq=r["seq"], event_type=r["event_type"],
                run_status=RunStatus(r["run_status"]), step_id=r["step_id"], attempt=r["attempt"],
                payload=json.loads(r["payload_json"]), timestamp=r["timestamp"],
                schema_version=r["schema_version"],
            )
            for r in rows
        ]
    # ── M4 REVIEW 审批单（高级节点：运行中人工闸门）──────────────────────────

    async def create_review(self, run_id: str, node_id: str, attempt: int,
                            *, title: str, note: str, review_id: str | None = None) -> str:
        """为 WAITING 的 REVIEW 步骤落一张审批单；同 run+node+attempt 幂等。"""
        rid = review_id or f"rev-{uuid4().hex[:12]}"
        await self.conn.execute(
            "INSERT OR IGNORE INTO wf_review"
            "(review_id, run_id, node_id, attempt, title, note, status, created_at) "
            "VALUES(?,?,?,?,?,?, 'pending', ?)",
            (rid, run_id, node_id, attempt, title, note, time.time()),
        )
        await self.conn.commit()
        return rid

    async def list_reviews(self, run_id: str) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM wf_review WHERE run_id=? ORDER BY created_at", (run_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_review(self, review_id: str) -> dict | None:
        cur = await self.conn.execute(
            "SELECT * FROM wf_review WHERE review_id=?", (review_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def resolve_review(self, review_id: str, decision: str, decided_by: str,
                             decision_note: str = "") -> str | None:
        """审批决策（单事务 CAS）：pending → approved/rejected + 步骤/run/事件联动。

        approved → 步骤 SUCCEEDED（run 保持 RUNNING，DAG 继续）；
        rejected → 步骤 FAILED + run FAILED（审批否决即停流）。
        已在决策冲突（重复决策/run 已终态）→ 返回 None，不做任何写入。
        成功后返回新的 run status 字符串。
        """
        row = await self.get_review(review_id)
        if row is None:
            return None
        run_id, node_id, attempt = row["run_id"], row["node_id"], row["attempt"]
        run = await self.get_run(run_id)
        if run is None or run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            return None  # run 已终态，审批单已失去意义
        step_status = StepStatus.SUCCEEDED if decision == "approve" else StepStatus.FAILED
        run_status = RunStatus.RUNNING if decision == "approve" else RunStatus.FAILED
        review_status = "approved" if decision == "approve" else "rejected"
        await self.conn.execute("BEGIN")
        try:
            cur = await self.conn.execute(
                "UPDATE wf_review SET status=?, decided_by=?, decision_note=?, decided_at=? "
                "WHERE review_id=? AND status='pending'",
                (review_status, decided_by, decision_note, time.time(), review_id),
            )
            if cur.rowcount != 1:
                await self.conn.rollback()
                return None
            cur2 = await self.conn.execute(
                "UPDATE wf_run SET status=?, lock_version=lock_version+1, updated_at=? "
                "WHERE run_id=? AND lock_version=?",
                (run_status.value, time.time(), run_id, run.lock_version),
            )
            if cur2.rowcount != 1:
                await self.conn.rollback()
                return None
            error_code = None if decision == "approve" else "REVIEW_REJECTED"
            error_message = None if decision == "approve" else (decision_note or "审批拒绝")
            await self.conn.execute(
                "UPDATE wf_step_run SET status=?, output_json=?, error_code=?, error_message=? "
                "WHERE run_id=? AND node_id=? AND attempt=?",
                (step_status.value, json.dumps({"decision": decision, "note": decision_note}),
                 error_code, error_message, run_id, node_id, attempt),
            )
            await self._insert_event(WorkflowRunEvent(
                run_id=run_id, seq=await self.next_seq(run_id),
                event_type=f"review_{decision}", run_status=run_status,
                step_id=node_id, attempt=attempt, timestamp=time.time(),
payload={"review_id": review_id, "decided_by": decided_by,
                 "note": decision_note},
            ))
            await self.conn.commit()
            return run_id
        except (aiosqlite.Error, json.JSONDecodeError, ValueError):
            await self.conn.rollback()
            raise

    async def pending_review_count(self, run_id: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM wf_review WHERE run_id=? AND status='pending'", (run_id,))
        row = await cur.fetchone()
        return int(row[0])

    # ── 转正运行时扩展（2026-08-22 决策"转正"）：驱动循环 / v1 桥接 / WebUI 视图 ──

    async def list_active_runs(self, limit: int = 50) -> list[WorkflowRun]:
        """非终态 run（queued/running/waiting_input/paused/cancelling）驱动轮询用。"""
        cur = await self.conn.execute(
            "SELECT * FROM wf_run WHERE status NOT IN ('succeeded','failed','cancelled') "
            "ORDER BY created_at LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [await self.get_run(r["run_id"]) for r in rows]

    async def list_runs_by_wf(self, workflow_id: str, limit: int = 200) -> list[WorkflowRun]:
        """WebUI 运行记录：按工作流倒序取最近运行。"""
        cur = await self.conn.execute(
            "SELECT run_id FROM wf_run WHERE workflow_id=? ORDER BY created_at DESC LIMIT ?",
            (workflow_id, limit),
        )
        rows = await cur.fetchall()
        return [await self.get_run(r["run_id"]) for r in rows]

    async def insert_revision(self, rev) -> None:
        """持久化一个不可变 revision（转正：publish 后立即固化）。"""
        await self.conn.execute(
            "INSERT OR IGNORE INTO wf_revision"
            "(revision_id, workflow_id, graph_json, content_hash, created_at) "
            "VALUES(?,?,?,?,?)",
            (rev.revision_id, rev.workflow_id, json.dumps(rev.model_dump(mode="json")),
             rev.content_hash or "", rev.created_at),
        )
        await self.conn.commit()

    async def get_revision(self, revision_id: str):
        cur = await self.conn.execute(
            "SELECT * FROM wf_revision WHERE revision_id=?", (revision_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        data = json.loads(row["graph_json"])
        return WorkflowRevision(**data)

    async def list_revisions(self, workflow_id: str, limit: int = 100) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT revision_id, content_hash, created_at FROM wf_revision "
            "WHERE workflow_id=? ORDER BY created_at DESC LIMIT ?", (workflow_id, limit)
        )
        rows = await cur.fetchall()
        return [
            {"revision_id": r["revision_id"], "content_hash": r["content_hash"],
             "created_at": r["created_at"]}
            for r in rows
        ]

    async def upsert_definition(self, *, workflow_id: str, name: str, description: str = "",
                                enabled: bool = True, current_revision_id: str | None = None) -> None:
        """v1 迁移首次落地 wf_definition（INSERT OR IGNORE 幂等）。"""
        await self.conn.execute(
            "INSERT OR IGNORE INTO wf_definition"
            "(workflow_id, name, description, enabled, current_revision_id, etag, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, name, description, int(enabled), current_revision_id,
             f"etag-{time.time():.0f}", time.time(), time.time()),
        )
        await self.conn.commit()

    async def set_current_revision(self, workflow_id: str, revision_id: str) -> None:
        """发布：原子置 current_revision_id + 翻转 etag（PATCH If-Match 语义下视为新版本）。"""
        await self.conn.execute(
            "UPDATE wf_definition SET current_revision_id=?, etag=?, updated_at=? "
            "WHERE workflow_id=?",
            (revision_id, f"etag-{int(time.time())}", time.time(), workflow_id),
        )
        await self.conn.commit()

    async def count_running_runs(self) -> int:
        """当前 RUNNING 数（M4 负载节流：driver 上限判定用，非终态轮询副作用为零）。"""
        cur = await self.conn.execute("SELECT COUNT(*) FROM wf_run WHERE status='running'")
        row = await cur.fetchone()
        return int(row[0])

    async def cancel_run(self, run_id: str) -> bool:
        """终止一个未终态的 run（幂等）：置 status=cancelled + 未完成 steps 置 cancelled + 事件。

        与 CAS 提交一致的事务性：取消判定 + 状态写入 + 事件在单事务内完成；
        已是终态的 run 不做任何变更并返回 False。
        """
        cur = await self.conn.execute(
            "SELECT status FROM wf_run WHERE run_id=?", (run_id,)
        )
        row = await cur.fetchone()
        if row is None or row["status"] in ("succeeded", "failed", "cancelled"):
            return False
        await self.conn.execute("BEGIN")
        try:
            await self.conn.execute(
                "UPDATE wf_run SET status='cancelled', updated_at=? WHERE run_id=?",
                (time.time(), run_id),
            )
            await self.conn.execute(
                "UPDATE wf_step_run SET status='cancelled' "
                "WHERE run_id=? AND status NOT IN ('succeeded','failed','cancelled')",
                (run_id,),
            )
            await self._insert_event(WorkflowRunEvent(
                run_id=run_id, seq=await self.next_seq(run_id),
                event_type="run_cancelled", run_status=RunStatus.CANCELLED,
                timestamp=time.time(),
            ))
            await self.conn.commit()
            return True
        except (aiosqlite.Error, ValueError):
            await self.conn.rollback()
            raise
