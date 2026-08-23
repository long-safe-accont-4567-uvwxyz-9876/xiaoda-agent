from __future__ import annotations

import json

import aiosqlite
import pytest

from db.db_memory_reconciliation import LeaseLostError, ReconciliationRepository, RepositoryConflictError
from memory.reconciliation_models import ReconciliationAction, ReconciliationDecision


async def _make_repo(*, with_edges: bool = True) -> tuple[aiosqlite.Connection, ReconciliationRepository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(
        """
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            is_raw INTEGER NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            importance REAL NOT NULL DEFAULT 0.5,
            phase TEXT NOT NULL DEFAULT 'new',
            stability REAL NOT NULL DEFAULT 1.0,
            rc INTEGER NOT NULL DEFAULT 0,
            superseded_by INTEGER
        )
        """
    )
    if with_edges:
        await conn.execute(
            """
            CREATE TABLE memory_edges (
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                edge_type TEXT NOT NULL,
                UNIQUE(source_id, target_id, edge_type)
            )
            """
        )
    await conn.commit()
    repo = ReconciliationRepository(conn)
    await repo.create_schema()
    return conn, repo


async def _seed_action(repo: ReconciliationRepository, conn: aiosqlite.Connection):
    rows = [
        (1, "u", "a", 1, "active", 9, "raw one", "{}", 0.5, "new", 1.0, 0, None),
        (2, "u", "a", 1, "active", 4, "raw two", "{}", 0.5, "new", 1.0, 0, None),
        (10, "u", "a", 0, "active", 3, "old ten", '{"ten":true}', 0.6, "warm", 2.0, 2, None),
        (11, "u", "a", 0, "active", 5, "old eleven", '{"eleven":true}', 0.7, "warm", 3.0, 3, None),
        (70, "u", "a", 0, "pending", 1, "candidate", '{"candidate":true}', 0.8, "new", 1.0, 0, None),
    ]
    await conn.executemany(
        "INSERT INTO episodic_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    await conn.commit()
    await repo.add_knowledge_source(10, 1)
    await repo.add_knowledge_source(11, 2)
    await repo.add_knowledge_source(70, 2)
    job_id = await repo.enqueue(70, user_id="u", agent_id="a", now=100.0)
    claim = await repo.claim_pending(user_id="u", agent_id="a", now=101.0, lease_seconds=30)
    assert claim is not None and claim.job_id == job_id
    return claim


def _decision(job_id: int, action: str, targets: list[int]) -> ReconciliationDecision:
    return ReconciliationDecision(
        job_id=job_id,
        action=ReconciliationAction(action),
        target_ids=targets,
        canonical_summary="canonical",
        confidence=0.8,
        reason="validated",
    )


async def _row(conn: aiosqlite.Connection, table: str, where: str, params: tuple = ()) -> dict | None:
    cursor = await conn.execute(f"SELECT * FROM {table} WHERE {where}", params)
    value = await cursor.fetchone()
    return dict(value) if value else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "initial_status", "expected_status"),
    [
        ("shadow", "active", "active"),
        ("enforce", "active", "provisional"),
    ],
)
async def test_register_candidate_sets_mode_aware_status_and_claim_expectation(
    mode: str,
    initial_status: str,
    expected_status: str,
) -> None:
    conn, repo = await _make_repo()
    try:
        await conn.execute(
            "INSERT INTO episodic_memories VALUES "
            "(70, 'u', 'a', 0, ?, 1, 'candidate', '{}', .5, 'new', 1, 0, NULL)",
            (initial_status,),
        )
        await conn.commit()

        job_id = await repo.register_candidate(
            70,
            1,
            user_id="u",
            agent_id="a",
            mode=mode,
            now=1,
        )
        candidate = await _row(conn, "episodic_memories", "id=70")
        claim = await repo.claim_pending(user_id="u", agent_id="a", now=2)

        assert candidate is not None and candidate["status"] == expected_status
        assert claim is not None and claim.job_id == job_id
        assert claim.candidate_expected_status == expected_status
        sources = await (
            await conn.execute(
                "SELECT raw_id FROM memory_knowledge_sources WHERE knowledge_id=70"
            )
        ).fetchall()
        assert [row["raw_id"] for row in sources] == [1]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_load_decision_input_fallback_is_bounded_and_returns_relevant_top_five() -> None:
    conn, repo = await _make_repo()
    try:
        rows = [
            (70, "u", "a", 0, "active", 1, "python async sqlite worker"),
            *[
                (
                    memory_id,
                    "u",
                    "a",
                    0,
                    "active",
                    1,
                    (
                        f"python async sqlite worker detail {memory_id}"
                        if memory_id < 20
                        else f"unrelated gardening note {memory_id}"
                    ),
                )
                for memory_id in range(10, 75)
                if memory_id != 70
            ],
            (90, "other", "a", 0, "active", 1, "python async sqlite worker other scope"),
        ]
        await conn.executemany(
            "INSERT INTO episodic_memories "
            "(id, user_id, agent_id, is_raw, status, version, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()
        await repo.register_candidate(
            70,
            1,
            user_id="u",
            agent_id="a",
            mode="shadow",
            now=1,
        )
        claim = await repo.claim_pending(user_id="u", agent_id="a", now=2)
        assert claim is not None

        decision_input = await repo.load_decision_input(claim)

        assert len(decision_input.candidates) == 5
        assert all(item["id"] < 20 for item in decision_input.candidates)
        assert [item["id"] for item in decision_input.candidates] == sorted(
            item["id"] for item in decision_input.candidates
        )
        assert set(decision_input.context.targets) == {
            item["id"] for item in decision_input.candidates
        }
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schema_is_idempotent_and_contains_contract_tables() -> None:
    conn, repo = await _make_repo()
    try:
        await repo.create_schema()
        names = {
            row[0]
            for row in await (
                await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert {
            "memory_knowledge_sources",
            "memory_reconciliation_jobs",
            "memory_reconciliation_actions",
            "memory_reconciliation_targets",
            "memory_reconciliation_snapshots",
            "memory_index_outbox",
            "memory_retrieval_epochs",
        } <= names
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_claim_is_scoped_and_exclusive() -> None:
    conn, repo = await _make_repo()
    try:
        await conn.executemany(
            "INSERT INTO episodic_memories VALUES (?, ?, ?, 0, 'pending', 1, ?, '{}', .5, 'new', 1, 0, NULL)",
            [(70, "u", "a", "one"), (71, "other", "a", "two")],
        )
        await conn.commit()
        first = await repo.enqueue(70, user_id="u", agent_id="a", now=10)
        assert await repo.enqueue(70, user_id="u", agent_id="a", now=11) == first
        await repo.enqueue(71, user_id="other", agent_id="a", now=10)

        claim = await repo.claim_pending(user_id="u", agent_id="a", now=12, lease_seconds=10)
        assert claim is not None and claim.candidate_memory_id == 70
        assert await repo.claim_pending(user_id="u", agent_id="a", now=13, lease_seconds=10) is None
        reclaimed = await repo.claim_pending(user_id="u", agent_id="a", now=23, lease_seconds=10)
        assert reclaimed is not None and reclaimed.lease_token != claim.lease_token
        assert await repo.complete_job(first, claim.lease_token, now=24) is False
        assert await repo.complete_job(first, reclaimed.lease_token, now=24) is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_two_repository_instances_cannot_hold_same_job_lease(tmp_path) -> None:
    db_path = tmp_path / "reconciliation.db"
    conn_one = await aiosqlite.connect(db_path)
    conn_two = await aiosqlite.connect(db_path)
    conn_one.row_factory = aiosqlite.Row
    conn_two.row_factory = aiosqlite.Row
    repo_one = ReconciliationRepository(conn_one)
    repo_two = ReconciliationRepository(conn_two)
    try:
        await conn_one.execute(
            """
            CREATE TABLE episodic_memories (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                is_raw INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                summary TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                importance REAL NOT NULL DEFAULT 0.5, phase TEXT NOT NULL DEFAULT 'new',
                stability REAL NOT NULL DEFAULT 1.0, rc INTEGER NOT NULL DEFAULT 0,
                superseded_by INTEGER
            )
            """
        )
        await conn_one.commit()
        await repo_one.create_schema()
        await conn_one.execute(
            "INSERT INTO episodic_memories VALUES (70, 'u', 'a', 0, 'pending', 1, 'candidate', '{}', .5, 'new', 1, 0, NULL)"
        )
        await conn_one.commit()
        await repo_one.enqueue(70, user_id="u", agent_id="a", now=1)

        first = await repo_one.claim_pending(user_id="u", agent_id="a", now=2, lease_seconds=10)
        blocked = await repo_two.claim_pending(user_id="u", agent_id="a", now=3, lease_seconds=10)
        reclaimed = await repo_two.claim_pending(user_id="u", agent_id="a", now=12, lease_seconds=10)

        assert first is not None
        assert blocked is None
        assert reclaimed is not None and reclaimed.lease_token != first.lease_token
    finally:
        await conn_one.close()
        await conn_two.close()


@pytest.mark.asyncio
async def test_fail_requires_token_and_schedules_retry() -> None:
    conn, repo = await _make_repo()
    try:
        await conn.execute(
            "INSERT INTO episodic_memories VALUES (70, 'u', 'a', 0, 'pending', 1, 'c', '{}', .5, 'new', 1, 0, NULL)"
        )
        await conn.commit()
        job_id = await repo.enqueue(70, user_id="u", agent_id="a", now=1)
        claim = await repo.claim_pending(user_id="u", agent_id="a", now=2, lease_seconds=10)
        assert claim is not None
        assert await repo.fail_job(job_id, "wrong", error="provider_failed", retry_delay=30, now=3) is False
        assert await repo.fail_job(job_id, claim.lease_token, error="provider_failed", retry_delay=30, now=3) is True
        assert await repo.claim_pending(user_id="u", agent_id="a", now=32, lease_seconds=10) is None
        retry = await repo.claim_pending(user_id="u", agent_id="a", now=33, lease_seconds=10)
        assert retry is not None and retry.retry_count == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "targets", "statuses", "versions", "source_expectations", "snapshot_count"),
    [
        ("store", [], {70: "active"}, {70: 2}, {70: {2}}, 0),
        ("skip", [], {70: "discarded"}, {70: 2}, {70: {2}}, 1),
        ("skip", [10], {10: "active", 70: "discarded"}, {10: 3, 70: 2}, {10: {1, 2}}, 1),
        ("update", [10], {10: "active", 70: "discarded"}, {10: 4, 70: 2}, {10: {1, 2}}, 2),
        (
            "merge",
            [10, 11],
            {10: "superseded", 11: "superseded", 70: "active"},
            {10: 4, 11: 6, 70: 2},
            {70: {1, 2}},
            3,
        ),
    ],
)
async def test_apply_actions_are_atomic_and_preserve_provenance(
    action: str,
    targets: list[int],
    statuses: dict[int, str],
    versions: dict[int, int],
    source_expectations: dict[int, set[int]],
    snapshot_count: int,
) -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        raw_before = await _row(conn, "episodic_memories", "id=1")

        action_id = await repo.apply_action(
            claim.job_id,
            claim.lease_token,
            _decision(claim.job_id, action, targets),
            expected_versions={10: 3, 11: 5},
            candidate_expected_version=claim.candidate_expected_version,
            candidate_expected_status=claim.candidate_expected_status,
            now=110,
        )

        for memory_id, status in statuses.items():
            memory = await _row(conn, "episodic_memories", "id=?", (memory_id,))
            assert memory is not None
            assert memory["status"] == status
            assert memory["version"] == versions[memory_id]
        if action in {"store", "update", "merge"}:
            canonical_id = 70 if action in {"store", "merge"} else 10
            assert (await _row(conn, "episodic_memories", "id=?", (canonical_id,)))["summary"] == "canonical"
        for knowledge_id, expected_sources in source_expectations.items():
            rows = await (
                await conn.execute(
                    "SELECT raw_id FROM memory_knowledge_sources WHERE knowledge_id=?", (knowledge_id,)
                )
            ).fetchall()
            assert {row[0] for row in rows} == expected_sources
        assert await _row(conn, "episodic_memories", "id=1") == raw_before
        assert (await _row(conn, "memory_reconciliation_actions", "id=?", (action_id,)))["executed"] == 1
        assert (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM memory_reconciliation_snapshots WHERE action_id=?", (action_id,)
                )
            ).fetchone()
        )[0] == snapshot_count
        assert (await _row(conn, "memory_retrieval_epochs", "user_id='u' AND agent_id='a'"))["epoch"] == 1
        assert (
            await (await conn.execute("SELECT COUNT(*) FROM memory_index_outbox")).fetchone()
        )[0] >= 1
    finally:
        await conn.close()


async def _candidate_snapshot(
    conn: aiosqlite.Connection, action_id: int, candidate_memory_id: int = 70
) -> dict | None:
    row = await _row(
        conn,
        "memory_reconciliation_snapshots",
        "action_id=? AND memory_id=?",
        (action_id, candidate_memory_id),
    )
    if row is None:
        return None
    return {
        "snapshot": json.loads(row["snapshot_json"]),
        "version": row["version"],
    }


@pytest.mark.asyncio
async def test_skip_snapshots_candidate_preimage_and_supports_full_restore() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        preimage = await _row(conn, "episodic_memories", "id=70")
        assert preimage is not None

        action_id = await repo.apply_action(
            claim.job_id,
            claim.lease_token,
            _decision(claim.job_id, "skip", [10]),
            expected_versions={10: 3},
            candidate_expected_version=claim.candidate_expected_version,
            candidate_expected_status=claim.candidate_expected_status,
            now=110,
        )

        discarded = await _row(conn, "episodic_memories", "id=70")
        assert discarded is not None
        assert discarded["status"] == "discarded"
        assert discarded["version"] == preimage["version"] + 1

        snap = await _candidate_snapshot(conn, action_id)
        assert snap is not None
        assert snap["version"] == preimage["version"]
        assert snap["snapshot"] == dict(preimage)

        assignments = ", ".join(f"{column}=?" for column in preimage)
        await conn.execute(
            f"UPDATE episodic_memories SET {assignments} WHERE id={preimage['id']}",
            tuple(preimage.values()),
        )
        await conn.commit()
        restored = await _row(conn, "episodic_memories", "id=?", (preimage["id"],))
        assert restored == preimage
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_update_and_merge_snapshot_candidate_preimage_alongside_targets() -> None:
    for action, targets in [("update", [10]), ("merge", [10, 11])]:
        conn, repo = await _make_repo()
        try:
            claim = await _seed_action(repo, conn)
            candidate_preimage = dict(await _row(conn, "episodic_memories", "id=70"))
            target_preimages = {
                memory_id: dict(await _row(conn, "episodic_memories", "id=?", (memory_id,)))
                for memory_id in targets
            }

            action_id = await repo.apply_action(
                claim.job_id,
                claim.lease_token,
                _decision(claim.job_id, action, targets),
                expected_versions={10: 3, 11: 5},
                candidate_expected_version=claim.candidate_expected_version,
                candidate_expected_status=claim.candidate_expected_status,
                now=110,
            )

            rows = await (
                await conn.execute(
                    "SELECT memory_id, version, snapshot_json FROM memory_reconciliation_snapshots "
                    "WHERE action_id=? ORDER BY memory_id",
                    (action_id,),
                )
            ).fetchall()
            by_memory = {int(row["memory_id"]): row for row in rows}
            assert set(by_memory) == {70, *targets}
            assert json.loads(by_memory[70]["snapshot_json"]) == candidate_preimage
            assert by_memory[70]["version"] == candidate_preimage["version"]
            for memory_id in targets:
                assert json.loads(by_memory[memory_id]["snapshot_json"]) == target_preimages[memory_id]
                assert by_memory[memory_id]["version"] == target_preimages[memory_id]["version"]
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_merge_records_targets_edges_and_superseded_by() -> None:
    conn, repo = await _make_repo(with_edges=True)
    try:
        claim = await _seed_action(repo, conn)
        action_id = await repo.apply_action(
            claim.job_id,
            claim.lease_token,
            _decision(claim.job_id, "merge", [10, 11]),
            expected_versions={10: 3, 11: 5},
            candidate_expected_version=claim.candidate_expected_version,
            candidate_expected_status=claim.candidate_expected_status,
            now=110,
        )
        assert (await _row(conn, "episodic_memories", "id=10"))["superseded_by"] == 70
        targets = await (
            await conn.execute(
                "SELECT target_memory_id FROM memory_reconciliation_targets WHERE action_id=? ORDER BY target_memory_id",
                (action_id,),
            )
        ).fetchall()
        assert [row[0] for row in targets] == [10, 11]
        edges = await (await conn.execute("SELECT source_id, target_id, edge_type FROM memory_edges")).fetchall()
        assert [tuple(row) for row in edges] == [(10, 70, "reconciled_into"), (11, 70, "reconciled_into")]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_candidate_changed_during_inference_fails_version_status_cas_without_writes() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        decision_input = await repo.load_decision_input(claim)
        assert claim.candidate_expected_version == 1
        assert claim.candidate_expected_status == "pending"
        assert decision_input.candidate_expected_version == 1
        assert decision_input.candidate_expected_status == "pending"

        await conn.execute(
            "UPDATE episodic_memories SET summary='concurrent edit', version=version+1 WHERE id=70"
        )
        await conn.commit()

        with pytest.raises(RepositoryConflictError, match="candidate"):
            await repo.apply_action(
                claim.job_id,
                claim.lease_token,
                _decision(claim.job_id, "store", []),
                expected_versions={},
                candidate_expected_version=decision_input.candidate_expected_version,
                candidate_expected_status=decision_input.candidate_expected_status,
                now=110,
            )

        candidate = await _row(conn, "episodic_memories", "id=70")
        assert candidate is not None
        assert candidate["summary"] == "concurrent edit"
        assert candidate["version"] == 2
        assert candidate["status"] == "pending"
        assert await _row(conn, "memory_reconciliation_actions", "job_id=?", (claim.job_id,)) is None
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_index_outbox")).fetchone())[0] == 0
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_retrieval_epochs")).fetchone())[0] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_rejects_stale_target_version_and_rolls_back() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        await conn.execute("UPDATE episodic_memories SET version=4 WHERE id=10")
        await conn.commit()

        with pytest.raises(RepositoryConflictError):
            await repo.apply_action(
                claim.job_id,
                claim.lease_token,
                _decision(claim.job_id, "update", [10]),
                expected_versions={10: 3},
                candidate_expected_version=claim.candidate_expected_version,
                candidate_expected_status=claim.candidate_expected_status,
                now=110,
            )

        assert (await _row(conn, "episodic_memories", "id=10"))["summary"] == "old ten"
        assert await _row(conn, "memory_reconciliation_actions", "job_id=?", (claim.job_id,)) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_rejects_raw_or_inactive_targets_at_cas_boundary() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        for target_id, version in [(1, 9), (10, 3)]:
            if target_id == 10:
                await conn.execute("UPDATE episodic_memories SET status='superseded' WHERE id=10")
                await conn.commit()
            with pytest.raises(RepositoryConflictError):
                await repo.apply_action(
                    claim.job_id,
                    claim.lease_token,
                    _decision(claim.job_id, "update", [target_id]),
                    expected_versions={target_id: version},
                    candidate_expected_version=claim.candidate_expected_version,
                    candidate_expected_status=claim.candidate_expected_status,
                    now=110,
                )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_shadow_records_proposal_without_side_effects() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        before = await _row(conn, "episodic_memories", "id=70")
        action_id = await repo.record_shadow(
            claim.job_id,
            claim.lease_token,
            _decision(claim.job_id, "merge", [10]),
            now=110,
        )
        assert await _row(conn, "episodic_memories", "id=70") == before
        assert (await _row(conn, "memory_reconciliation_actions", "id=?", (action_id,)))["executed"] == 0
        assert (await _row(conn, "memory_reconciliation_jobs", "id=?", (claim.job_id,)))["status"] == "shadow"
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_index_outbox")).fetchone())[0] == 0
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_retrieval_epochs")).fetchone())[0] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_failure_during_action_rolls_back_every_write() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        await conn.execute(
            """
            CREATE TRIGGER fail_outbox BEFORE INSERT ON memory_index_outbox
            BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END
            """
        )
        await conn.commit()

        with pytest.raises(aiosqlite.IntegrityError, match="forced outbox failure"):
            await repo.apply_action(
                claim.job_id,
                claim.lease_token,
                _decision(claim.job_id, "update", [10]),
                expected_versions={10: 3},
                candidate_expected_version=claim.candidate_expected_version,
                candidate_expected_status=claim.candidate_expected_status,
                now=110,
            )

        assert (await _row(conn, "episodic_memories", "id=10"))["summary"] == "old ten"
        assert (await _row(conn, "episodic_memories", "id=70"))["status"] == "pending"
        assert await _row(conn, "memory_reconciliation_actions", "job_id=?", (claim.job_id,)) is None
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_retrieval_epochs")).fetchone())[0] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_outbox_dedupe_lease_recovery_retry_and_dead() -> None:
    conn, repo = await _make_repo()
    try:
        await repo.enqueue_outbox("same", memory_id=10, operation="upsert", payload={"id": 10}, now=1)
        await repo.enqueue_outbox("same", memory_id=10, operation="upsert", payload={"id": 10}, now=2)
        assert (await (await conn.execute("SELECT COUNT(*) FROM memory_index_outbox")).fetchone())[0] == 1

        first = await repo.claim_outbox(now=3, lease_seconds=10, limit=1)
        assert len(first) == 1
        assert await repo.claim_outbox(now=4, lease_seconds=10, limit=1) == []
        recovered = await repo.claim_outbox(now=14, lease_seconds=10, limit=1)
        assert recovered[0].lease_token != first[0].lease_token
        assert await repo.complete_outbox(first[0].outbox_id, first[0].lease_token, now=15) is False
        assert await repo.fail_outbox(
            recovered[0].outbox_id, recovered[0].lease_token, error="index_failed", now=15, max_retries=2
        ) == "retry"
        retry = await repo.claim_outbox(now=16, lease_seconds=10, limit=1)
        assert await repo.fail_outbox(
            retry[0].outbox_id, retry[0].lease_token, error="index_failed", now=17, max_retries=2
        ) == "dead"
        await repo.enqueue_outbox("done", memory_id=11, operation="delete", payload={"id": 11}, now=18)
        done_claim = await repo.claim_outbox(now=19, lease_seconds=10, limit=1)
        assert await repo.complete_outbox(done_claim[0].outbox_id, done_claim[0].lease_token, now=20) is True
        assert (await _row(conn, "memory_index_outbox", "dedupe_key='done'"))["status"] == "done"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_load_decision_input_filters_candidate_pool_by_scope_and_state() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        await conn.executemany(
            "INSERT INTO episodic_memories VALUES (?, ?, ?, ?, ?, 1, ?, '{}', .5, 'new', 1, 0, NULL)",
            [
                (20, "other", "a", 0, "active", "wrong user"),
                (21, "u", "other", 0, "active", "wrong agent"),
                (22, "u", "a", 1, "active", "raw"),
                (23, "u", "a", 0, "superseded", "inactive"),
            ],
        )
        await conn.commit()

        decision_input = await repo.load_decision_input(claim)

        assert decision_input.candidate["id"] == 70
        assert {item["id"] for item in decision_input.candidates} == {10}
        assert set(decision_input.context.targets) == {10}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_requires_current_job_lease() -> None:
    conn, repo = await _make_repo()
    try:
        claim = await _seed_action(repo, conn)
        with pytest.raises(LeaseLostError):
            await repo.apply_action(
                claim.job_id,
                "wrong-token",
                _decision(claim.job_id, "store", []),
                expected_versions={},
                candidate_expected_version=claim.candidate_expected_version,
                candidate_expected_status=claim.candidate_expected_status,
            )
    finally:
        await conn.close()
