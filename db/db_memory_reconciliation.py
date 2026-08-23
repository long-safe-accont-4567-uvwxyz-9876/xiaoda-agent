from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import aiosqlite

from memory.reconciliation_models import (
    DecisionValidationContext,
    MemoryIdentity,
    ReconciliationAction,
    ReconciliationDecision,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_knowledge_sources (
    knowledge_id INTEGER NOT NULL,
    raw_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (knowledge_id, raw_id)
);

CREATE TABLE IF NOT EXISTS memory_reconciliation_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_memory_id INTEGER NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_token TEXT,
    lease_expires REAL,
    available_at REAL NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    candidate_expected_version INTEGER,
    candidate_expected_status TEXT,
    decision_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_jobs_claim
    ON memory_reconciliation_jobs (user_id, agent_id, status, available_at, lease_expires);

CREATE TABLE IF NOT EXISTS memory_reconciliation_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    proposed_action TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    executed INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_reconciliation_targets (
    action_id INTEGER NOT NULL,
    target_memory_id INTEGER NOT NULL,
    PRIMARY KEY (action_id, target_memory_id)
);

CREATE TABLE IF NOT EXISTS memory_reconciliation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL,
    memory_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (action_id, memory_id)
);

CREATE TABLE IF NOT EXISTS memory_index_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    memory_id INTEGER NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_token TEXT,
    lease_expires REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_memory_index_outbox_claim
    ON memory_index_outbox (status, lease_expires, id);

CREATE TABLE IF NOT EXISTS memory_retrieval_epochs (
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, agent_id)
);
"""


async def create_schema(conn: aiosqlite.Connection) -> None:
    """Create the additive v32 reconciliation schema on an existing connection."""
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            await conn.execute(statement)


class _TransactionContext(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None: ...


TransactionFactory = Callable[[], _TransactionContext]


class RepositoryConflictError(RuntimeError):
    pass


class LeaseLostError(RepositoryConflictError):
    pass


@dataclass(frozen=True, slots=True)
class ReconciliationJobClaim:
    job_id: int
    candidate_memory_id: int
    user_id: str
    agent_id: str
    lease_token: str
    lease_expires: float
    retry_count: int
    candidate_expected_version: int
    candidate_expected_status: str


@dataclass(frozen=True, slots=True)
class ReconciliationDecisionInput:
    claim: ReconciliationJobClaim
    candidate: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    context: DecisionValidationContext
    candidate_expected_version: int
    candidate_expected_status: str


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    outbox_id: int
    dedupe_key: str
    memory_id: int
    operation: str
    payload: dict[str, Any]
    lease_token: str
    lease_expires: float
    retry_count: int


class ReconciliationRepository:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        transaction_factory: TransactionFactory | None = None,
    ) -> None:
        self._conn = conn
        self._transaction_factory = transaction_factory

    async def create_schema(self) -> None:
        await create_schema(self._conn)
        await self._conn.commit()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        if self._transaction_factory is not None:
            async with self._transaction_factory():
                yield
            return
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await self._conn.rollback()
            raise
        else:
            await self._conn.commit()

    async def add_knowledge_source(
        self,
        knowledge_id: int,
        raw_id: int,
        *,
        now: float | None = None,
        transactional: bool = True,
    ) -> None:
        recorded_at = time.time() if now is None else now
        if transactional:
            async with self._transaction():
                await self._conn.execute(
                    "INSERT OR IGNORE INTO memory_knowledge_sources "
                    "(knowledge_id, raw_id, created_at) VALUES (?, ?, ?)",
                    (knowledge_id, raw_id, recorded_at),
                )
            return
        await self._conn.execute(
            "INSERT OR IGNORE INTO memory_knowledge_sources "
            "(knowledge_id, raw_id, created_at) VALUES (?, ?, ?)",
            (knowledge_id, raw_id, recorded_at),
        )

    async def register_candidate(
        self,
        candidate_memory_id: int,
        raw_id: int,
        *,
        user_id: str,
        agent_id: str,
        mode: str = "shadow",
        now: float | None = None,
        transactional: bool = True,
    ) -> int:
        """Atomically set candidate visibility, provenance, and reconciliation job."""
        normalized_mode = str(getattr(mode, "value", mode)).strip().lower()
        if normalized_mode not in {"shadow", "enforce"}:
            raise ValueError("reconciliation mode must be shadow or enforce")
        recorded_at = time.time() if now is None else now

        async def _write() -> int:
            row = await (
                await self._conn.execute(
                    "SELECT status FROM episodic_memories "
                    "WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0",
                    (candidate_memory_id, user_id, agent_id),
                )
            ).fetchone()
            if row is None:
                raise RepositoryConflictError("candidate is missing or outside scope")
            current_status = str(row["status"])
            if normalized_mode == "shadow":
                if current_status != "active":
                    raise RepositoryConflictError("shadow candidate must remain active")
            elif current_status == "active":
                cursor = await self._conn.execute(
                    "UPDATE episodic_memories SET status='provisional' "
                    "WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0 AND status='active'",
                    (candidate_memory_id, user_id, agent_id),
                )
                if cursor.rowcount != 1:
                    raise RepositoryConflictError("candidate status changed during registration")
            elif current_status != "provisional":
                raise RepositoryConflictError("enforce candidate must be active or provisional")

            await self._conn.execute(
                "INSERT OR IGNORE INTO memory_knowledge_sources "
                "(knowledge_id, raw_id, created_at) VALUES (?, ?, ?)",
                (candidate_memory_id, raw_id, recorded_at),
            )
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO memory_reconciliation_jobs
                    (candidate_memory_id, user_id, agent_id, status, available_at, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    candidate_memory_id,
                    user_id,
                    agent_id,
                    recorded_at,
                    recorded_at,
                    recorded_at,
                ),
            )
            job_row = await (
                await self._conn.execute(
                    "SELECT id, user_id, agent_id FROM memory_reconciliation_jobs "
                    "WHERE candidate_memory_id=?",
                    (candidate_memory_id,),
                )
            ).fetchone()
            if (
                job_row is None
                or job_row["user_id"] != user_id
                or job_row["agent_id"] != agent_id
            ):
                raise RepositoryConflictError("candidate is already enqueued under another scope")
            return int(job_row["id"])

        if transactional:
            async with self._transaction():
                return await _write()
        return await _write()

    async def enqueue(
        self,
        candidate_memory_id: int,
        *,
        user_id: str,
        agent_id: str,
        now: float | None = None,
    ) -> int:
        recorded_at = time.time() if now is None else now
        async with self._transaction():
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO memory_reconciliation_jobs
                    (candidate_memory_id, user_id, agent_id, status, available_at, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (candidate_memory_id, user_id, agent_id, recorded_at, recorded_at, recorded_at),
            )
            row = await (
                await self._conn.execute(
                    "SELECT id, user_id, agent_id FROM memory_reconciliation_jobs WHERE candidate_memory_id=?",
                    (candidate_memory_id,),
                )
            ).fetchone()
            if row is None or row["user_id"] != user_id or row["agent_id"] != agent_id:
                raise RepositoryConflictError("candidate is already enqueued under another scope")
            return int(row["id"])

    async def claim_pending(
        self,
        *,
        user_id: str,
        agent_id: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
    ) -> ReconciliationJobClaim | None:
        claimed_at = time.time() if now is None else now
        lease_token = uuid.uuid4().hex
        lease_expires = claimed_at + lease_seconds
        async with self._transaction():
            row = await (
                await self._conn.execute(
                    """
                    SELECT j.id, j.candidate_memory_id, j.user_id, j.agent_id, j.retry_count,
                           m.version AS candidate_expected_version,
                           m.status AS candidate_expected_status
                    FROM memory_reconciliation_jobs AS j
                    JOIN episodic_memories AS m
                      ON m.id=j.candidate_memory_id
                     AND m.user_id=j.user_id
                     AND m.agent_id=j.agent_id
                     AND m.is_raw=0
                     AND m.status IN ('active', 'pending', 'provisional')
                    WHERE j.user_id=? AND j.agent_id=? AND j.available_at<=?
                      AND (
                        j.status IN ('pending', 'retry')
                        OR (j.status='processing' AND j.lease_expires<=?)
                      )
                    ORDER BY j.id
                    LIMIT 1
                    """,
                    (user_id, agent_id, claimed_at, claimed_at),
                )
            ).fetchone()
            if row is None:
                return None
            cursor = await self._conn.execute(
                """
                UPDATE memory_reconciliation_jobs
                SET status='processing', lease_token=?, lease_expires=?, updated_at=?,
                    candidate_expected_version=?, candidate_expected_status=?
                WHERE id=? AND (
                    status IN ('pending', 'retry')
                    OR (status='processing' AND lease_expires<=?)
                )
                """,
                (
                    lease_token,
                    lease_expires,
                    claimed_at,
                    row["candidate_expected_version"],
                    row["candidate_expected_status"],
                    row["id"],
                    claimed_at,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return ReconciliationJobClaim(
                job_id=int(row["id"]),
                candidate_memory_id=int(row["candidate_memory_id"]),
                user_id=str(row["user_id"]),
                agent_id=str(row["agent_id"]),
                lease_token=lease_token,
                lease_expires=lease_expires,
                retry_count=int(row["retry_count"]),
                candidate_expected_version=int(row["candidate_expected_version"]),
                candidate_expected_status=str(row["candidate_expected_status"]),
            )

    async def complete_job(self, job_id: int, lease_token: str, *, now: float | None = None) -> bool:
        completed_at = time.time() if now is None else now
        async with self._transaction():
            cursor = await self._conn.execute(
                """
                UPDATE memory_reconciliation_jobs
                SET status='completed', lease_token=NULL, lease_expires=NULL,
                    updated_at=?, completed_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (completed_at, completed_at, job_id, lease_token),
            )
            return cursor.rowcount == 1

    async def fail_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        error: str,
        retry_delay: float,
        now: float | None = None,
    ) -> bool:
        failed_at = time.time() if now is None else now
        async with self._transaction():
            cursor = await self._conn.execute(
                """
                UPDATE memory_reconciliation_jobs
                SET status='retry', lease_token=NULL, lease_expires=NULL,
                    retry_count=retry_count+1, available_at=?, error=?, updated_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (failed_at + retry_delay, error, failed_at, job_id, lease_token),
            )
            return cursor.rowcount == 1

    async def release_lease(self, job_id: int, lease_token: str, *, now: float | None = None) -> bool:
        released_at = time.time() if now is None else now
        async with self._transaction():
            cursor = await self._conn.execute(
                """
                UPDATE memory_reconciliation_jobs
                SET status='retry', lease_token=NULL, lease_expires=NULL,
                    available_at=?, updated_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (released_at, released_at, job_id, lease_token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _char_bigrams(text: str) -> set[str]:
        normalized = re.sub(r"\s+", "", text.casefold())
        if not normalized:
            return set()
        if len(normalized) == 1:
            return {normalized}
        return {normalized[index : index + 2] for index in range(len(normalized) - 1)}

    async def _load_relevant_candidates(
        self,
        claim: ReconciliationJobClaim,
        candidate_summary: str,
    ) -> tuple[dict[str, Any], ...]:
        from db.fts_utils import _build_fts_query

        fts_available = await (
            await self._conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='episodic_memory_fts'"
            )
        ).fetchone()
        if fts_available is not None:
            fts_query = _build_fts_query(candidate_summary)
            if not fts_query:
                return ()
            try:
                rows = await (
                    await self._conn.execute(
                        """
                        SELECT em.*, bm25(episodic_memory_fts) AS score
                        FROM episodic_memory_fts
                        JOIN episodic_memories AS em ON em.id=episodic_memory_fts.id
                        WHERE episodic_memory_fts MATCH ?
                          AND em.user_id=? AND em.agent_id=?
                          AND em.is_raw=0 AND em.status='active' AND em.id<>?
                        ORDER BY score ASC, em.id ASC
                        LIMIT 5
                        """,
                        (
                            fts_query,
                            claim.user_id,
                            claim.agent_id,
                            claim.candidate_memory_id,
                        ),
                    )
                ).fetchall()
                return tuple(dict(row) for row in rows)
            except aiosqlite.OperationalError:
                pass

        rows = await (
            await self._conn.execute(
                """
                SELECT * FROM episodic_memories
                WHERE user_id=? AND agent_id=? AND is_raw=0
                  AND status='active' AND id<>?
                ORDER BY id ASC
                LIMIT 50
                """,
                (claim.user_id, claim.agent_id, claim.candidate_memory_id),
            )
        ).fetchall()
        query_bigrams = self._char_bigrams(candidate_summary)
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for row in rows:
            candidate = dict(row)
            candidate_bigrams = self._char_bigrams(str(candidate.get("summary", "")))
            union = query_bigrams | candidate_bigrams
            similarity = (
                len(query_bigrams & candidate_bigrams) / len(union) if union else 0.0
            )
            if similarity > 0:
                scored.append((similarity, int(candidate["id"]), candidate))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in scored[:5])

    async def load_decision_input(self, claim: ReconciliationJobClaim) -> ReconciliationDecisionInput:
        candidate_row = await (
            await self._conn.execute(
                """
                SELECT * FROM episodic_memories
                WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0
                  AND version=? AND status=?
                """,
                (
                    claim.candidate_memory_id,
                    claim.user_id,
                    claim.agent_id,
                    claim.candidate_expected_version,
                    claim.candidate_expected_status,
                ),
            )
        ).fetchone()
        if candidate_row is None:
            raise RepositoryConflictError("candidate is missing or outside scope")
        candidate = dict(candidate_row)
        candidates = await self._load_relevant_candidates(
            claim,
            str(candidate.get("summary", "")),
        )
        identities = {
            int(row["id"]): MemoryIdentity(
                memory_id=int(row["id"]),
                user_id=str(row["user_id"]),
                agent_id=str(row["agent_id"]),
                is_raw=bool(row["is_raw"]),
                status=str(row["status"]),
                version=int(row["version"]),
            )
            for row in candidates
        }
        context = DecisionValidationContext(
            job_id=claim.job_id,
            user_id=claim.user_id,
            agent_id=claim.agent_id,
            candidate=MemoryIdentity(
                memory_id=int(candidate["id"]),
                user_id=str(candidate["user_id"]),
                agent_id=str(candidate["agent_id"]),
                is_raw=bool(candidate["is_raw"]),
                status=str(candidate["status"]),
                version=int(candidate["version"]),
            ),
            targets=identities,
        )
        return ReconciliationDecisionInput(
            claim=claim,
            candidate=candidate,
            candidates=candidates,
            context=context,
            candidate_expected_version=claim.candidate_expected_version,
            candidate_expected_status=claim.candidate_expected_status,
        )

    async def _require_lease(self, job_id: int, lease_token: str) -> aiosqlite.Row:
        row = await (
            await self._conn.execute(
                """
                SELECT * FROM memory_reconciliation_jobs
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (job_id, lease_token),
            )
        ).fetchone()
        if row is None:
            raise LeaseLostError("reconciliation job lease was lost")
        return row

    async def _insert_action(
        self,
        decision: ReconciliationDecision,
        *,
        executed: bool,
        now: float,
    ) -> int:
        decision_json = decision.model_dump_json()
        cursor = await self._conn.execute(
            """
            INSERT INTO memory_reconciliation_actions
                (job_id, proposed_action, decision_json, executed, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (decision.job_id, decision.action.value, decision_json, int(executed), now),
        )
        action_id = int(cursor.lastrowid)
        await self._conn.executemany(
            "INSERT INTO memory_reconciliation_targets (action_id, target_memory_id) VALUES (?, ?)",
            [(action_id, target_id) for target_id in decision.target_ids],
        )
        return action_id

    async def record_shadow(
        self,
        job_id: int,
        lease_token: str,
        decision: ReconciliationDecision,
        *,
        now: float | None = None,
    ) -> int:
        recorded_at = time.time() if now is None else now
        async with self._transaction():
            await self._require_lease(job_id, lease_token)
            action_id = await self._insert_action(decision, executed=False, now=recorded_at)
            cursor = await self._conn.execute(
                """
                UPDATE memory_reconciliation_jobs
                SET status='shadow', decision_json=?, lease_token=NULL, lease_expires=NULL,
                    updated_at=?, completed_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (decision.model_dump_json(), recorded_at, recorded_at, job_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("reconciliation job lease was lost")
            return action_id

    async def _load_targets(
        self,
        decision: ReconciliationDecision,
        *,
        user_id: str,
        agent_id: str,
        expected_versions: Mapping[int, int],
    ) -> dict[int, dict[str, Any]]:
        if not decision.target_ids:
            return {}
        placeholders = ",".join("?" for _ in decision.target_ids)
        rows = await (
            await self._conn.execute(
                f"SELECT * FROM episodic_memories WHERE id IN ({placeholders})",
                tuple(decision.target_ids),
            )
        ).fetchall()
        targets = {int(row["id"]): dict(row) for row in rows}
        for target_id in decision.target_ids:
            target = targets.get(target_id)
            if (
                target is None
                or target["user_id"] != user_id
                or target["agent_id"] != agent_id
                or int(target["is_raw"]) != 0
                or target["status"] != "active"
                or target_id not in expected_versions
                or int(target["version"]) != expected_versions[target_id]
            ):
                raise RepositoryConflictError("target failed scope, state, or version CAS")
        return targets

    async def _snapshot(self, action_id: int, memory: Mapping[str, Any], now: float) -> None:
        """Persist the full pre-image of one memory row for the given action.

        Called symmetrically for targets and for the candidate itself so that
        discarded (SKIP/UPDATE) or rewritten (MERGE) candidate content stays
        recoverable. Snapshot role is derivable without extra columns:
        memory_id equal to the job's candidate_memory_id marks the candidate.
        """
        await self._conn.execute(
            """
            INSERT INTO memory_reconciliation_snapshots
                (action_id, memory_id, version, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                action_id,
                memory["id"],
                memory["version"],
                json.dumps(dict(memory), ensure_ascii=False, allow_nan=False, sort_keys=True),
                now,
            ),
        )

    async def _union_sources(self, destination_id: int, source_ids: list[int], now: float) -> None:
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        await self._conn.execute(
            f"""
            INSERT OR IGNORE INTO memory_knowledge_sources (knowledge_id, raw_id, created_at)
            SELECT ?, raw_id, ? FROM memory_knowledge_sources
            WHERE knowledge_id IN ({placeholders})
            """,
            (destination_id, now, *source_ids),
        )

    async def _has_memory_edges(self) -> bool:
        row = await (
            await self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_edges'"
            )
        ).fetchone()
        return row is not None

    async def _record_supersession_edge(
        self, source_id: int, target_id: int, now: float
    ) -> None:
        columns = {
            row[1]
            for row in await self._conn.execute_fetchall(
                "PRAGMA table_info(memory_edges)"
            )
        }
        if {"source_memory_id", "target_memory_id"} <= columns:
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO memory_edges
                    (source_memory_id, target_memory_id, edge_type, created_at, updated_at)
                VALUES (?, ?, 'supersedes', ?, ?)
                """,
                (source_id, target_id, now, now),
            )
        elif {"source_id", "target_id"} <= columns:
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO memory_edges (source_id, target_id, edge_type)
                VALUES (?, ?, 'reconciled_into')
                """,
                (target_id, source_id),
            )

    async def _enqueue_action_outbox(
        self,
        action_id: int,
        changes: list[tuple[int, str]],
        now: float,
    ) -> None:
        await self._conn.executemany(
            """
            INSERT OR IGNORE INTO memory_index_outbox
                (dedupe_key, memory_id, operation, payload_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            [
                (
                    f"reconciliation:{action_id}:{memory_id}:{operation}",
                    memory_id,
                    operation,
                    json.dumps({"memory_id": memory_id}, sort_keys=True),
                    now,
                    now,
                )
                for memory_id, operation in changes
            ],
        )

    async def _increment_epoch(self, user_id: str, agent_id: str, now: float) -> None:
        await self._conn.execute(
            """
            INSERT INTO memory_retrieval_epochs (user_id, agent_id, epoch, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, agent_id) DO UPDATE
            SET epoch=memory_retrieval_epochs.epoch+1, updated_at=excluded.updated_at
            """,
            (user_id, agent_id, now),
        )

    async def apply_action(
        self,
        job_id: int,
        lease_token: str,
        decision: ReconciliationDecision,
        *,
        expected_versions: Mapping[int, int],
        candidate_expected_version: int,
        candidate_expected_status: str,
        now: float | None = None,
    ) -> int:
        applied_at = time.time() if now is None else now
        async with self._transaction():
            job = await self._require_lease(job_id, lease_token)
            if decision.job_id != job_id:
                raise RepositoryConflictError("decision job id mismatch")
            if (
                candidate_expected_version != job["candidate_expected_version"]
                or candidate_expected_status != job["candidate_expected_status"]
                or candidate_expected_status not in {"active", "pending", "provisional"}
            ):
                raise RepositoryConflictError("candidate expectation does not match claimed job")
            candidate_row = await (
                await self._conn.execute(
                    """
                    SELECT * FROM episodic_memories
                    WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0
                      AND version=? AND status=?
                    """,
                    (
                        job["candidate_memory_id"],
                        job["user_id"],
                        job["agent_id"],
                        candidate_expected_version,
                        candidate_expected_status,
                    ),
                )
            ).fetchone()
            if candidate_row is None:
                raise RepositoryConflictError("candidate failed scope or state CAS")
            candidate = dict(candidate_row)
            targets = await self._load_targets(
                decision,
                user_id=str(job["user_id"]),
                agent_id=str(job["agent_id"]),
                expected_versions=expected_versions,
            )
            action_id = await self._insert_action(decision, executed=True, now=applied_at)
            changes: list[tuple[int, str]] = []
            candidate_id = int(candidate["id"])

            if decision.action is ReconciliationAction.STORE:
                candidate_cursor = await self._conn.execute(
                    """
                    UPDATE episodic_memories SET summary=?, status='active', version=version+1
                    WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0
                      AND version=? AND status=?
                    """,
                    (
                        decision.canonical_summary,
                        candidate_id,
                        job["user_id"],
                        job["agent_id"],
                        candidate_expected_version,
                        candidate_expected_status,
                    ),
                )
                if candidate_cursor.rowcount != 1:
                    raise RepositoryConflictError("candidate version or status CAS failed")
                changes.append((candidate_id, "upsert"))
            elif decision.action is ReconciliationAction.SKIP:
                await self._snapshot(action_id, candidate, applied_at)
                candidate_cursor = await self._conn.execute(
                    """
                    UPDATE episodic_memories SET status='discarded', version=version+1
                    WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0
                      AND version=? AND status=?
                    """,
                    (
                        candidate_id,
                        job["user_id"],
                        job["agent_id"],
                        candidate_expected_version,
                        candidate_expected_status,
                    ),
                )
                if candidate_cursor.rowcount != 1:
                    raise RepositoryConflictError("candidate version or status CAS failed")
                if decision.target_ids:
                    await self._union_sources(decision.target_ids[0], [candidate_id], applied_at)
                    changes.append((decision.target_ids[0], "upsert"))
                changes.append((candidate_id, "delete"))
            elif decision.action is ReconciliationAction.UPDATE:
                await self._snapshot(action_id, candidate, applied_at)
                candidate_cursor = await self._conn.execute(
                    """
                    UPDATE episodic_memories SET status='discarded', version=version+1
                    WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0
                      AND version=? AND status=?
                    """,
                    (
                        candidate_id,
                        job["user_id"],
                        job["agent_id"],
                        candidate_expected_version,
                        candidate_expected_status,
                    ),
                )
                if candidate_cursor.rowcount != 1:
                    raise RepositoryConflictError("candidate version or status CAS failed")
                target_id = decision.target_ids[0]
                await self._snapshot(action_id, targets[target_id], applied_at)
                cursor = await self._conn.execute(
                    """
                    UPDATE episodic_memories SET summary=?, version=version+1
                    WHERE id=? AND version=? AND status='active' AND is_raw=0
                    """,
                    (decision.canonical_summary, target_id, expected_versions[target_id]),
                )
                if cursor.rowcount != 1:
                    raise RepositoryConflictError("target version CAS failed")
                await self._union_sources(target_id, [candidate_id], applied_at)
                changes.extend([(target_id, "upsert"), (candidate_id, "delete")])
            else:
                await self._snapshot(action_id, candidate, applied_at)
                candidate_cursor = await self._conn.execute(
                    """
                    UPDATE episodic_memories SET summary=?, status='active', version=version+1
                    WHERE id=? AND user_id=? AND agent_id=? AND is_raw=0
                      AND version=? AND status=?
                    """,
                    (
                        decision.canonical_summary,
                        candidate_id,
                        job["user_id"],
                        job["agent_id"],
                        candidate_expected_version,
                        candidate_expected_status,
                    ),
                )
                if candidate_cursor.rowcount != 1:
                    raise RepositoryConflictError("candidate version or status CAS failed")
                await self._union_sources(candidate_id, decision.target_ids, applied_at)
                edges_exist = await self._has_memory_edges()
                for target_id in decision.target_ids:
                    await self._snapshot(action_id, targets[target_id], applied_at)
                    cursor = await self._conn.execute(
                        """
                        UPDATE episodic_memories
                        SET status='superseded', superseded_by=?, version=version+1
                        WHERE id=? AND version=? AND status='active' AND is_raw=0
                        """,
                        (candidate_id, target_id, expected_versions[target_id]),
                    )
                    if cursor.rowcount != 1:
                        raise RepositoryConflictError("target version CAS failed")
                    if edges_exist:
                        await self._record_supersession_edge(
                            candidate_id, target_id, applied_at
                        )
                    changes.append((target_id, "delete"))
                changes.append((candidate_id, "upsert"))

            await self._enqueue_action_outbox(action_id, changes, applied_at)
            await self._increment_epoch(str(job["user_id"]), str(job["agent_id"]), applied_at)
            cursor = await self._conn.execute(
                """
                UPDATE memory_reconciliation_jobs
                SET status='completed', decision_json=?, lease_token=NULL, lease_expires=NULL,
                    error=NULL, updated_at=?, completed_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (decision.model_dump_json(), applied_at, applied_at, job_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError("reconciliation job lease was lost")
            return action_id

    async def get_retrieval_epoch(self, user_id: str, agent_id: str) -> int:
        row = await (
            await self._conn.execute(
                "SELECT epoch FROM memory_retrieval_epochs WHERE user_id=? AND agent_id=?",
                (user_id, agent_id),
            )
        ).fetchone()
        return int(row[0]) if row else 0

    async def enqueue_outbox(
        self,
        dedupe_key: str,
        *,
        memory_id: int,
        operation: str,
        payload: Mapping[str, Any],
        now: float | None = None,
    ) -> None:
        recorded_at = time.time() if now is None else now
        payload_json = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False, sort_keys=True)
        async with self._transaction():
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO memory_index_outbox
                    (dedupe_key, memory_id, operation, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (dedupe_key, memory_id, operation, payload_json, recorded_at, recorded_at),
            )

    async def claim_outbox(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = 60.0,
        limit: int = 100,
    ) -> list[OutboxClaim]:
        claimed_at = time.time() if now is None else now
        claims: list[OutboxClaim] = []
        async with self._transaction():
            rows = await (
                await self._conn.execute(
                    """
                    SELECT * FROM memory_index_outbox
                    WHERE status IN ('pending', 'retry')
                       OR (status='processing' AND lease_expires<=?)
                    ORDER BY id LIMIT ?
                    """,
                    (claimed_at, limit),
                )
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                expires = claimed_at + lease_seconds
                cursor = await self._conn.execute(
                    """
                    UPDATE memory_index_outbox
                    SET status='processing', lease_token=?, lease_expires=?, updated_at=?
                    WHERE id=? AND (
                        status IN ('pending', 'retry')
                        OR (status='processing' AND lease_expires<=?)
                    )
                    """,
                    (token, expires, claimed_at, row["id"], claimed_at),
                )
                if cursor.rowcount == 1:
                    claims.append(
                        OutboxClaim(
                            outbox_id=int(row["id"]),
                            dedupe_key=str(row["dedupe_key"]),
                            memory_id=int(row["memory_id"]),
                            operation=str(row["operation"]),
                            payload=json.loads(row["payload_json"]),
                            lease_token=token,
                            lease_expires=expires,
                            retry_count=int(row["retry_count"]),
                        )
                    )
        return claims

    async def complete_outbox(
        self,
        outbox_id: int,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        completed_at = time.time() if now is None else now
        async with self._transaction():
            cursor = await self._conn.execute(
                """
                UPDATE memory_index_outbox
                SET status='done', lease_token=NULL, lease_expires=NULL,
                    updated_at=?, completed_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (completed_at, completed_at, outbox_id, lease_token),
            )
            return cursor.rowcount == 1

    async def fail_outbox(
        self,
        outbox_id: int,
        lease_token: str,
        *,
        error: str,
        now: float | None = None,
        max_retries: int = 3,
    ) -> str | None:
        failed_at = time.time() if now is None else now
        async with self._transaction():
            row = await (
                await self._conn.execute(
                    """
                    SELECT retry_count FROM memory_index_outbox
                    WHERE id=? AND status='processing' AND lease_token=?
                    """,
                    (outbox_id, lease_token),
                )
            ).fetchone()
            if row is None:
                return None
            retry_count = int(row["retry_count"]) + 1
            status = "dead" if retry_count >= max_retries else "retry"
            await self._conn.execute(
                """
                UPDATE memory_index_outbox
                SET status=?, lease_token=NULL, lease_expires=NULL,
                    retry_count=?, error=?, updated_at=?
                WHERE id=? AND status='processing' AND lease_token=?
                """,
                (status, retry_count, error, failed_at, outbox_id, lease_token),
            )
            return status
