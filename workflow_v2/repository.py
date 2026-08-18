from __future__ import annotations

import json
import time

import aiosqlite

from workflow_v2.models import (
    RunStatus,
    StepStatus,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowStepRun,
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
        except Exception:
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
        except Exception:
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
        except Exception:
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
        except Exception:
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
