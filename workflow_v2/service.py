"""Workflow V2 application service: business operations over WorkflowRepository.

The router talks only to this service via ``request.app.state.workflow_v2``.
All reads/writes go through ``WorkflowRepository`` (plus a couple of direct
``wf_definition``/``wf_revision`` queries — those tables ship in the Task 3
DDL, see db/db_workflow.py).
"""
from __future__ import annotations

import sqlite3
import time
import uuid

from workflow_v2.models import RunStatus, WorkflowRun, WorkflowRunEvent
from workflow_v2.repository import WorkflowRepository

# statuses that can no longer be cancelled
_TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


def _run_dict(run: WorkflowRun) -> dict:
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "revision_id": run.revision_id,
        "status": run.status.value,
        "lock_version": run.lock_version,
        "parent_run_id": run.parent_run_id,
        "idempotency_key": run.idempotency_key,
        "input": run.input,
        "output": run.output,
        "cancel_requested_at": run.cancel_requested_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


class WorkflowV2Service:
    def __init__(self, repo: WorkflowRepository):
        self.repo = repo

    # --- definitions -------------------------------------------------------

    async def _definition_row(self, wf_id: str):
        cur = await self.repo.conn.execute(
            "SELECT * FROM wf_definition WHERE workflow_id=?", (wf_id,)
        )
        return await cur.fetchone()

    async def get_definition(self, wf_id: str) -> dict | None:
        row = await self._definition_row(wf_id)
        if row is None:
            return None
        return {
            "workflow_id": row["workflow_id"],
            "name": row["name"],
            "description": row["description"],
            "enabled": bool(row["enabled"]),
            "current_revision_id": row["current_revision_id"],
            "etag": row["etag"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def patch_definition(self, wf_id: str, body: dict, etag: str) -> dict | None:
        """Update identity fields (name/description) guarded by an atomic etag CAS.

        The etag comparison happens inside the SAME UPDATE statement (``WHERE
        workflow_id=? AND etag=?``), so two concurrent PATCHes with the same
        If-Match can never both pass: exactly one sees ``rowcount == 1`` and the
        loser gets 0 rows. Returns the updated definition (fresh etag) on
        success, or ``None`` when the etag moved / the definition is gone —
        the route maps that to 409 ETAG_CONFLICT.
        """
        sets: list[str] = []
        params: list = []
        if "name" in body:
            sets.append("name=?")
            params.append(body["name"])
        if "description" in body:
            sets.append("description=?")
            params.append(body["description"])
        sets.append("etag=?")
        params.append(f"etag-{uuid.uuid4().hex[:12]}")
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(wf_id)
        params.append(etag)
        cur = await self.repo.conn.execute(
            f"UPDATE wf_definition SET {', '.join(sets)} WHERE workflow_id=? AND etag=?",
            params,
        )
        if cur.rowcount != 1:
            return None  # etag moved (or definition deleted) — no blind overwrite
        await self.repo.conn.commit()
        return await self.get_definition(wf_id)

    # --- runs ----------------------------------------------------------------

    async def _find_run_by_idempotency(self, wf_id: str, idempotency_key: str) -> WorkflowRun | None:
        cur = await self.repo.conn.execute(
            "SELECT run_id FROM wf_run WHERE workflow_id=? AND idempotency_key=?",
            (wf_id, idempotency_key),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return await self.repo.get_run(row["run_id"])

    async def create_or_get_run(self, wf_id: str, input_: dict, idempotency_key: str) -> dict | None:
        """Idempotent: same (workflow_id, idempotency_key) always returns the same run.

        Returns None when the workflow definition does not exist.
        """
        definition = await self._definition_row(wf_id)
        if definition is None:
            return None
        existing = await self._find_run_by_idempotency(wf_id, idempotency_key)
        if existing is not None:
            return _run_dict(existing)

        now = time.time()
        run = WorkflowRun(
            run_id=f"wfr_{uuid.uuid4().hex[:12]}",
            workflow_id=wf_id,
            revision_id=definition["current_revision_id"] or "",
            status=RunStatus.QUEUED,
            idempotency_key=idempotency_key,
            input=input_,
            created_at=now,
            updated_at=now,
        )
        event = WorkflowRunEvent(
            run_id=run.run_id, seq=1, event_type="run_queued",
            run_status=RunStatus.QUEUED, timestamp=now,
        )
        try:
            await self.repo.create_run(run, [], event)
        except sqlite3.IntegrityError:
            # lost a concurrent-create race on ux_wf_run_idem: return the winner
            existing = await self._find_run_by_idempotency(wf_id, idempotency_key)
            if existing is not None:
                return _run_dict(existing)
            raise
        return _run_dict(run)

    async def snapshot(self, run_id: str) -> dict | None:
        run = await self.repo.get_run(run_id)
        if run is None:
            return None
        steps = await self.repo.list_steps(run_id)
        last_seq = (await self.repo.next_seq(run_id)) - 1
        return {
            "run": _run_dict(run),
            "steps": [s.model_dump(mode="json") for s in steps],
            "last_seq": last_seq,
        }

    async def events_after(self, run_id: str, after_seq: int) -> list[dict]:
        events = await self.repo.events_after(run_id, after_seq)
        return [e.model_dump(mode="json") for e in events]

    async def request_cancel(self, run_id: str) -> dict:
        """Idempotent cancel request. Raises KeyError when the run does not exist."""
        run = await self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in _TERMINAL_STATUSES:
            return {"cancel_requested": False, "status": run.status.value}
        now = time.time()
        if run.cancel_requested_at is None:
            await self.repo.conn.execute(
                "UPDATE wf_run SET cancel_requested_at=?, updated_at=? WHERE run_id=?",
                (now, now, run_id),
            )
            await self.repo.conn.commit()
        return {"cancel_requested": True, "status": run.status.value}
