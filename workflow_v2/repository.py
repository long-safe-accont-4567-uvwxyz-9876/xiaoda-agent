from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import uuid4

import aiosqlite

from workflow_v2.models import (
    RunStatus,
    StepStatus,
    WorkflowRevision,
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowStepRun,
)

_TERMINAL_RUN_STATUSES = (
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
)


class _TransactionConflict(Exception):
    pass


class WorkflowRepository:
    """Serialize all access to the repository's single SQLite connection."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        """The repository's single shared connection (read helpers only)."""
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run one write unit under the connection lock and own its commit."""
        async with self._lock:
            await self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                await self._conn.rollback()
                raise
            else:
                await self._conn.commit()

    @staticmethod
    def _run_from_row(row: Any) -> WorkflowRun:
        return WorkflowRun(
            run_id=row["run_id"], workflow_id=row["workflow_id"],
            revision_id=row["revision_id"], status=RunStatus(row["status"]),
            lock_version=row["lock_version"], parent_run_id=row["parent_run_id"],
            idempotency_key=row["idempotency_key"],
            input=json.loads(row["input_json"]), output=json.loads(row["output_json"]),
            cancel_requested_at=row["cancel_requested_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _step_from_row(row: Any) -> WorkflowStepRun:
        return WorkflowStepRun(
            run_id=row["run_id"], node_id=row["node_id"], attempt=row["attempt"],
            status=StepStatus(row["status"]), input=json.loads(row["input_json"]),
            output=json.loads(row["output_json"]), error_code=row["error_code"],
            error_message=row["error_message"], lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )

    @staticmethod
    def _event_from_row(row: Any) -> WorkflowRunEvent:
        return WorkflowRunEvent(
            run_id=row["run_id"], seq=row["seq"], event_type=row["event_type"],
            run_status=RunStatus(row["run_status"]), step_id=row["step_id"],
            attempt=row["attempt"], payload=json.loads(row["payload_json"]),
            timestamp=row["timestamp"], schema_version=row["schema_version"],
        )

    async def _get_run(self, run_id: str) -> WorkflowRun | None:
        cur = await self._conn.execute("SELECT * FROM wf_run WHERE run_id=?", (run_id,))
        row = await cur.fetchone()
        return self._run_from_row(row) if row else None

    async def _next_seq(self, run_id: str) -> int:
        cur = await self._conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM wf_run_event WHERE run_id=?",
            (run_id,),
        )
        row = await cur.fetchone()
        return int(row[0])

    async def _insert_step(self, step: WorkflowStepRun) -> None:
        await self._conn.execute(
            """INSERT INTO wf_step_run(run_id, node_id, attempt, status, input_json,
                output_json, error_code, error_message, lease_owner, lease_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (step.run_id, step.node_id, step.attempt, step.status.value,
             json.dumps(step.input), json.dumps(step.output), step.error_code,
             step.error_message, step.lease_owner, step.lease_expires_at),
        )

    async def _insert_event(self, event: WorkflowRunEvent) -> None:
        await self._conn.execute(
            """INSERT INTO wf_run_event(run_id, seq, event_type, run_status, step_id,
                attempt, payload_json, timestamp, schema_version)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (event.run_id, event.seq, event.event_type, event.run_status.value,
             event.step_id, event.attempt, json.dumps(event.payload),
             event.timestamp or time.time(), event.schema_version),
        )

    async def _live_review_id(
        self, run_id: str, node_id: str, attempt: int,
    ) -> str | None:
        cur = await self._conn.execute(
            "SELECT review_id FROM wf_review "
            "WHERE run_id=? AND node_id=? AND attempt=? AND status<>'superseded' "
            "ORDER BY created_at ASC, rowid ASC LIMIT 1",
            (run_id, node_id, attempt),
        )
        row = await cur.fetchone()
        return str(row["review_id"]) if row else None

    async def _insert_review(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        *,
        title: str,
        note: str,
        review_id: str | None = None,
    ) -> tuple[str, bool]:
        # One live review per (run, node, attempt): reuse the existing one
        # instead of stacking duplicates (matches ux_wf_review_attempt).
        existing = await self._live_review_id(run_id, node_id, attempt)
        if existing is not None:
            return existing, False
        candidate = review_id or f"rev-{uuid4().hex[:12]}"
        try:
            await self._conn.execute(
                "INSERT INTO wf_review"
                "(review_id, run_id, node_id, attempt, title, note, status, created_at) "
                "VALUES(?,?,?,?,?,?, 'pending', ?)",
                (candidate, run_id, node_id, attempt, title, note, time.time()),
            )
        except sqlite3.IntegrityError:
            # Concurrent caller won the slot → return its canonical id.
            existing = await self._live_review_id(run_id, node_id, attempt)
            if existing is not None:
                return existing, False
            raise
        return candidate, True

    async def create_run(
        self,
        run: WorkflowRun,
        steps: list[WorkflowStepRun],
        first_event: WorkflowRunEvent,
    ) -> None:
        async with self.transaction():
            await self._conn.execute(
                """INSERT INTO wf_run(run_id, workflow_id, revision_id, status,
                    lock_version, parent_run_id, idempotency_key, input_json,
                    output_json, cancel_requested_at, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run.run_id, run.workflow_id, run.revision_id, run.status.value,
                 run.lock_version, run.parent_run_id, run.idempotency_key,
                 json.dumps(run.input), json.dumps(run.output),
                 run.cancel_requested_at, run.created_at, run.updated_at),
            )
            for step in steps:
                await self._insert_step(step)
            await self._insert_event(first_event)

    async def next_seq(self, run_id: str) -> int:
        async with self._lock:
            return await self._next_seq(run_id)

    async def append_event(self, event: WorkflowRunEvent) -> int:
        async with self.transaction():
            if not event.seq:  # seq=0/None → auto-assign; explicit seq stays
                event = event.model_copy(update={
                    "seq": await self._next_seq(event.run_id),
                })
            await self._insert_event(event)
        return event.seq

    async def _claim_step(
        self,
        run_id: str,
        node_id: str,
        expected_lock: int,
        lease_owner: str,
        lease_ttl: float,
        event: WorkflowRunEvent | None,
    ) -> WorkflowStepRun | None:
        now = time.time()
        async with self.transaction():
            cur = await self._conn.execute(
                "UPDATE wf_run SET lock_version=lock_version+1, status=?, updated_at=? "
                "WHERE run_id=? AND lock_version=? "
                "AND status NOT IN ('succeeded','failed','cancelled')",
                (RunStatus.RUNNING.value, now, run_id, expected_lock),
            )
            if cur.rowcount != 1:
                return None

            latest_cur = await self._conn.execute(
                "SELECT * FROM wf_step_run WHERE run_id=? AND node_id=? "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id, node_id),
            )
            latest = await latest_cur.fetchone()
            if latest is not None and latest["status"] == StepStatus.PENDING.value:
                attempt = int(latest["attempt"])
                await self._conn.execute(
                    "UPDATE wf_step_run SET status=?, lease_owner=?, lease_expires_at=?, "
                    "error_code=NULL, error_message=NULL WHERE run_id=? AND node_id=? "
                    "AND attempt=? AND status=?",
                    (StepStatus.RUNNING.value, lease_owner, now + lease_ttl,
                     run_id, node_id, attempt, StepStatus.PENDING.value),
                )
                step = self._step_from_row(latest).model_copy(update={
                    "status": StepStatus.RUNNING,
                    "lease_owner": lease_owner,
                    "lease_expires_at": now + lease_ttl,
                    "error_code": None,
                    "error_message": None,
                })
            else:
                attempt = int(latest["attempt"]) + 1 if latest is not None else 1
                step = WorkflowStepRun(
                    run_id=run_id, node_id=node_id, attempt=attempt,
                    status=StepStatus.RUNNING, lease_owner=lease_owner,
                    lease_expires_at=now + lease_ttl,
                )
                await self._insert_step(step)

            if event is not None:
                await self._insert_event(event.model_copy(update={
                    "seq": await self._next_seq(run_id),
                    "step_id": node_id,
                    "attempt": attempt,
                }))
            return step

    async def claim_step(
        self,
        run_id: str,
        node_id: str,
        expected_lock: int,
        lease_owner: str,
        lease_ttl: float,
    ) -> WorkflowStepRun | None:
        return await self._claim_step(
            run_id, node_id, expected_lock, lease_owner, lease_ttl, None,
        )

    async def claim_step_with_event(
        self,
        run_id: str,
        node_id: str,
        expected_lock: int,
        lease_owner: str,
        lease_ttl: float,
        event: WorkflowRunEvent,
    ) -> WorkflowStepRun | None:
        return await self._claim_step(
            run_id, node_id, expected_lock, lease_owner, lease_ttl, event,
        )

    async def commit_step_result(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        step_status: StepStatus,
        step_patch: dict,
        run_status: RunStatus,
        expected_lock: int,
        event: WorkflowRunEvent,
        *,
        review: dict | None = None,
    ) -> bool:
        if step_status == StepStatus.WAITING_INPUT and review is None:
            raise ValueError("WAITING_INPUT transition requires a review")
        try:
            async with self.transaction():
                cur = await self._conn.execute(
                    "UPDATE wf_run SET status=?, lock_version=lock_version+1, updated_at=? "
                    "WHERE run_id=? AND lock_version=? "
                    "AND status NOT IN ('succeeded','failed','cancelled')",
                    (run_status.value, time.time(), run_id, expected_lock),
                )
                if cur.rowcount != 1:
                    raise _TransactionConflict

                if node_id != "__run__":
                    step_cur = await self._conn.execute(
                        "UPDATE wf_step_run SET status=?, output_json=?, error_code=?, "
                        "error_message=? WHERE run_id=? AND node_id=? AND attempt=? "
                        "AND status IN ('running','pending')",
                        (step_status.value, json.dumps(step_patch.get("output", {})),
                         step_patch.get("error_code"), step_patch.get("error_message"),
                         run_id, node_id, attempt),
                    )
                    if step_cur.rowcount != 1:
                        raise _TransactionConflict

                await self._insert_event(event.model_copy(update={
                    "seq": await self._next_seq(run_id),
                    "step_id": None if node_id == "__run__" else node_id,
                    "attempt": None if node_id == "__run__" else attempt,
                }))
                if review is not None:
                    await self._insert_review(
                        run_id, node_id, attempt,
                        title=str(review.get("title") or "人工审批"),
                        note=str(review.get("note") or ""),
                        review_id=review.get("review_id"),
                    )
        except _TransactionConflict:
            return False
        return True

    async def get_run(self, run_id: str) -> WorkflowRun | None:
        async with self._lock:
            return await self._get_run(run_id)

    async def find_run_by_idempotency(
        self, workflow_id: str, idempotency_key: str,
    ) -> WorkflowRun | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_run WHERE workflow_id=? AND idempotency_key=?",
                (workflow_id, idempotency_key),
            )
            row = await cur.fetchone()
            return self._run_from_row(row) if row else None

    async def list_steps(self, run_id: str) -> list[WorkflowStepRun]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_step_run WHERE run_id=? ORDER BY node_id, attempt",
                (run_id,),
            )
            return [self._step_from_row(row) for row in await cur.fetchall()]

    async def events_after(
        self, run_id: str, after_seq: int,
    ) -> list[WorkflowRunEvent]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_run_event WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            )
            return [self._event_from_row(row) for row in await cur.fetchall()]

    async def create_review(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        *,
        title: str,
        note: str,
        review_id: str | None = None,
    ) -> str:
        async with self.transaction():
            canonical_id, _ = await self._insert_review(
                run_id, node_id, attempt, title=title, note=note,
                review_id=review_id,
            )
            return canonical_id

    async def list_reviews(self, run_id: str) -> list[dict]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_review WHERE run_id=? AND status<>'superseded' "
                "ORDER BY created_at",
                (run_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def get_review(self, review_id: str) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_review WHERE review_id=? AND status<>'superseded'",
                (review_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def resolve_review(
        self,
        review_id: str,
        decision: str,
        decided_by: str,
        decision_note: str = "",
    ) -> str | None:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        try:
            async with self.transaction():
                cur = await self._conn.execute(
                    "SELECT * FROM wf_review WHERE review_id=? AND status='pending'",
                    (review_id,),
                )
                review = await cur.fetchone()
                if review is None:
                    raise _TransactionConflict
                run = await self._get_run(review["run_id"])
                if run is None or run.status.value in _TERMINAL_RUN_STATUSES:
                    raise _TransactionConflict

                review_status = "approved" if decision == "approve" else "rejected"
                review_cur = await self._conn.execute(
                    "UPDATE wf_review SET status=?, decided_by=?, decision_note=?, "
                    "decided_at=? WHERE review_id=? AND status='pending'",
                    (review_status, decided_by, decision_note, time.time(), review_id),
                )
                if review_cur.rowcount != 1:
                    raise _TransactionConflict

                step_status = (
                    StepStatus.SUCCEEDED if decision == "approve" else StepStatus.FAILED
                )
                run_status = (
                    RunStatus.RUNNING if decision == "approve" else RunStatus.FAILED
                )
                run_cur = await self._conn.execute(
                    "UPDATE wf_run SET status=?, lock_version=lock_version+1, updated_at=? "
                    "WHERE run_id=? AND lock_version=? "
                    "AND status NOT IN ('succeeded','failed','cancelled')",
                    (run_status.value, time.time(), run.run_id, run.lock_version),
                )
                if run_cur.rowcount != 1:
                    raise _TransactionConflict

                error_code = None if decision == "approve" else "REVIEW_REJECTED"
                error_message = (
                    None if decision == "approve" else (decision_note or "审批拒绝")
                )
                step_cur = await self._conn.execute(
                    "UPDATE wf_step_run SET status=?, output_json=?, error_code=?, "
                    "error_message=? WHERE run_id=? AND node_id=? AND attempt=? "
                    "AND status='waiting_input'",
                    (step_status.value,
                     json.dumps({"decision": decision, "note": decision_note}),
                     error_code, error_message, review["run_id"], review["node_id"],
                     review["attempt"]),
                )
                if step_cur.rowcount != 1:
                    raise _TransactionConflict

                await self._insert_event(WorkflowRunEvent(
                    run_id=run.run_id, seq=await self._next_seq(run.run_id),
                    event_type=f"review_{decision}", run_status=run_status,
                    step_id=review["node_id"], attempt=review["attempt"],
                    timestamp=time.time(), payload={
                        "review_id": review_id, "decided_by": decided_by,
                        "note": decision_note,
                    },
                ))
                return run.run_id
        except _TransactionConflict:
            return None

    async def pending_review_count(self, run_id: str) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM wf_review WHERE run_id=? AND status='pending'",
                (run_id,),
            )
            row = await cur.fetchone()
            return int(row[0])

    async def list_active_runs(self, limit: int = 50) -> list[WorkflowRun]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_run WHERE status NOT IN "
                "('succeeded','failed','cancelled') ORDER BY created_at LIMIT ?",
                (limit,),
            )
            return [self._run_from_row(row) for row in await cur.fetchall()]

    async def list_runs_by_wf(
        self, workflow_id: str, limit: int = 200,
    ) -> list[WorkflowRun]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_run WHERE workflow_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (workflow_id, limit),
            )
            return [self._run_from_row(row) for row in await cur.fetchall()]

    async def insert_revision(self, revision: WorkflowRevision) -> None:
        async with self.transaction():
            await self._conn.execute(
                "INSERT OR IGNORE INTO wf_revision"
                "(revision_id, workflow_id, graph_json, content_hash, created_at) "
                "VALUES(?,?,?,?,?)",
                (revision.revision_id, revision.workflow_id,
                 json.dumps(revision.model_dump(mode="json")),
                 revision.content_hash or "", revision.created_at),
            )

    async def get_revision(self, revision_id: str) -> WorkflowRevision | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_revision WHERE revision_id=?", (revision_id,),
            )
            row = await cur.fetchone()
            return WorkflowRevision(**json.loads(row["graph_json"])) if row else None

    async def list_revisions(self, workflow_id: str, limit: int = 100) -> list[dict]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT revision_id, content_hash, created_at FROM wf_revision "
                "WHERE workflow_id=? ORDER BY created_at DESC LIMIT ?",
                (workflow_id, limit),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def find_revision_by_hash(
        self, workflow_id: str, content_hash: str,
    ) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT revision_id, content_hash FROM wf_revision "
                "WHERE workflow_id=? AND content_hash=? "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow_id, content_hash),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def revision_exists(self, workflow_id: str, revision_id: str) -> bool:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT 1 FROM wf_revision WHERE revision_id=? AND workflow_id=?",
                (revision_id, workflow_id),
            )
            return (await cur.fetchone()) is not None

    async def upsert_definition(
        self,
        *,
        workflow_id: str,
        name: str,
        description: str = "",
        enabled: bool = True,
        current_revision_id: str | None = None,
    ) -> None:
        now = time.time()
        async with self.transaction():
            await self._conn.execute(
                "INSERT OR IGNORE INTO wf_definition"
                "(workflow_id, name, description, enabled, current_revision_id, etag, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (workflow_id, name, description, int(enabled), current_revision_id,
                 f"etag-{now:.0f}", now, now),
            )

    async def get_definition(self, workflow_id: str) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM wf_definition WHERE workflow_id=?", (workflow_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def patch_definition(
        self,
        workflow_id: str,
        body: dict,
        expected_etag: str,
        new_etag: str,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if "name" in body:
            sets.append("name=?")
            params.append(body["name"])
        if "description" in body:
            sets.append("description=?")
            params.append(body["description"])
        sets.extend(("etag=?", "updated_at=?"))
        params.extend((new_etag, time.time(), workflow_id, expected_etag))
        async with self.transaction():
            cur = await self._conn.execute(
                f"UPDATE wf_definition SET {', '.join(sets)} "
                "WHERE workflow_id=? AND etag=?",
                params,
            )
            return cur.rowcount == 1

    async def set_current_revision(self, workflow_id: str, revision_id: str) -> None:
        async with self.transaction():
            await self._conn.execute(
                "UPDATE wf_definition SET current_revision_id=?, etag=?, updated_at=? "
                "WHERE workflow_id=?",
                (revision_id, f"etag-{uuid4().hex[:12]}", time.time(), workflow_id),
            )

    async def set_current_revision_cas(
        self,
        workflow_id: str,
        revision_id: str,
        expected_etag: str,
    ) -> bool:
        async with self.transaction():
            cur = await self._conn.execute(
                "UPDATE wf_definition SET current_revision_id=?, etag=?, updated_at=? "
                "WHERE workflow_id=? AND etag=?",
                (revision_id, f"etag-{uuid4().hex[:12]}", time.time(),
                 workflow_id, expected_etag),
            )
            return cur.rowcount == 1

    async def get_config(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            try:
                cur = await self._conn.execute(
                    "SELECT value FROM wf_config WHERE key=?", (key,),
                )
                row = await cur.fetchone()
            except sqlite3.OperationalError:
                return default
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    async def set_config(self, key: str, value: Any) -> None:
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO wf_config(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    async def count_running_runs(self) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM wf_run WHERE status='running'"
            )
            row = await cur.fetchone()
            return int(row[0])

    async def request_run_cancel(
        self, run_id: str,
    ) -> tuple[WorkflowRun | None, bool]:
        async with self.transaction():
            run = await self._get_run(run_id)
            if run is None:
                return None, False
            if run.status.value in _TERMINAL_RUN_STATUSES:
                return run, False
            if run.cancel_requested_at is None:
                now = time.time()
                await self._conn.execute(
                    "UPDATE wf_run SET cancel_requested_at=?, updated_at=? "
                    "WHERE run_id=? AND status NOT IN ('succeeded','failed','cancelled')",
                    (now, now, run_id),
                )
                run = run.model_copy(update={
                    "cancel_requested_at": now, "updated_at": now,
                })
            return run, True

    async def cancel_run(self, run_id: str) -> bool:
        try:
            async with self.transaction():
                cur = await self._conn.execute(
                    "UPDATE wf_run SET status='cancelled', lock_version=lock_version+1, "
                    "updated_at=? WHERE run_id=? "
                    "AND status NOT IN ('succeeded','failed','cancelled')",
                    (time.time(), run_id),
                )
                if cur.rowcount != 1:
                    raise _TransactionConflict
                await self._conn.execute(
                    "UPDATE wf_step_run SET status='cancelled' WHERE run_id=? "
                    "AND status NOT IN ('succeeded','failed','cancelled')",
                    (run_id,),
                )
                await self._insert_event(WorkflowRunEvent(
                    run_id=run_id, seq=await self._next_seq(run_id),
                    event_type="run_cancelled", run_status=RunStatus.CANCELLED,
                    timestamp=time.time(),
                ))
        except _TransactionConflict:
            return False
        return True
