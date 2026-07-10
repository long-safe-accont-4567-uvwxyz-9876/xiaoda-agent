"""TDD test for Bug 9: DB migration v12 hash chain not backfilled.

Tests that migration v12 backfills content_hash and version for existing
episodic_memories rows, and creates valid hash chain entries in memory_versions.

Run: python -m pytest tests/test_db_migration_hash.py -x -v
"""
import hashlib
import sys
import time
from pathlib import Path

import aiosqlite
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.asyncio


def _compute_content_hash(summary: str) -> str:
    """SHA-256 of memory summary (mirrors context_governance.compute_content_hash)."""
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


async def _build_pre_v12_db(db_path: str, summaries: list[str]) -> None:
    """Build a DB at schema v11 state (no content_hash/version columns,
    no memory_versions table) and insert rows with the given summaries.

    This simulates a database that existed before v12 migration was applied.
    """
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            emotion_label TEXT DEFAULT '',
            session_id TEXT DEFAULT 'user',
            embedding_id INTEGER DEFAULT -1,
            rag_status TEXT DEFAULT 'pending',
            rag_synced_at REAL DEFAULT 0,
            doc_id TEXT DEFAULT '',
            source TEXT DEFAULT 'user',
            access_count INTEGER DEFAULT 0,
            distilled INTEGER DEFAULT 0,
            entities TEXT DEFAULT '',
            event_type TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}'
        );
    """)
    now = time.time()
    for s in summaries:
        await conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
            (now, s),
        )
    await conn.commit()
    await conn.close()


async def _init_db(db_path: str):
    """Open DB with DatabaseManager.init() so v12 migration runs."""
    from db.database import DatabaseManager
    db = DatabaseManager(db_path=db_path)
    await db.init()
    return db


# ── Tests ──────────────────────────────────────────────

async def test_v12_backfills_nonempty_content_hash(tmp_path):
    """After v12 migration, existing rows must have non-empty content_hash
    matching SHA-256(summary)."""
    summaries = [
        "用户喜欢吃枣椰蜜糖",
        "讨论了 PLC 编程方案",
        "用户决定采用 Python 3.12",
    ]
    db_path = str(tmp_path / "test_v12_hash.db")
    await _build_pre_v12_db(db_path, summaries)

    db = await _init_db(db_path)
    try:
        cur = await db._conn.execute(
            "SELECT id, summary, content_hash FROM episodic_memories ORDER BY id"
        )
        rows = await cur.fetchall()
        assert len(rows) == 3
        for row in rows:
            content_hash = row["content_hash"]
            assert content_hash, (
                f"memory id={row['id']} content_hash 为空 — 迁移未回填"
            )
            expected = _compute_content_hash(row["summary"])
            assert content_hash == expected, (
                f"memory id={row['id']} content_hash 不匹配: "
                f"expected {expected[:12]}, got {content_hash[:12]}"
            )
    finally:
        await db.close()


async def test_v12_sets_version_to_1(tmp_path):
    """After v12 migration, existing rows must have version=1."""
    summaries = ["记忆 A", "记忆 B"]
    db_path = str(tmp_path / "test_v12_version.db")
    await _build_pre_v12_db(db_path, summaries)

    db = await _init_db(db_path)
    try:
        cur = await db._conn.execute(
            "SELECT id, version FROM episodic_memories ORDER BY id"
        )
        rows = await cur.fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["version"] == 1, (
                f"memory id={row['id']} version={row['version']}, expected 1"
            )
    finally:
        await db.close()


async def test_v12_backfills_valid_hash_chain(tmp_path):
    """After v12 migration, memory_versions has valid v1 hash chain entries
    for existing rows (prev_hash='', content_hash matches summary snapshot)."""
    from memory.context_governance import ContextGovernance

    summaries = [
        "用户喜欢吃枣椰蜜糖",
        "讨论了 PLC 编程方案",
        "用户决定采用 Python 3.12",
    ]
    db_path = str(tmp_path / "test_v12_chain.db")
    await _build_pre_v12_db(db_path, summaries)

    db = await _init_db(db_path)
    try:
        gov = ContextGovernance(conn=db._conn)
        cur = await db._conn.execute("SELECT id FROM episodic_memories ORDER BY id")
        mem_ids = [r["id"] for r in await cur.fetchall()]
        assert len(mem_ids) == 3

        # verify_hash_chain should pass for every backfilled memory
        for mid in mem_ids:
            result = await gov.verify_hash_chain(mid)
            assert result["valid"], (
                f"memory {mid} 哈希链无效: {result['detail']}"
            )
            assert result["versions"] == 1, (
                f"memory {mid} 版本数={result['versions']}, expected 1"
            )

        # memory_versions table should have one v1 entry per existing memory
        cur = await db._conn.execute(
            "SELECT memory_id, version, prev_hash, content_hash, summary_snapshot "
            "FROM memory_versions ORDER BY memory_id"
        )
        mv_rows = await cur.fetchall()
        assert len(mv_rows) == 3, (
            f"memory_versions 应有 3 条回填记录, 实际 {len(mv_rows)}"
        )
        for mv in mv_rows:
            assert mv["version"] == 1
            assert mv["prev_hash"] == "", (
                f"memory {mv['memory_id']} v1 prev_hash 应为空, "
                f"实际 {mv['prev_hash']!r}"
            )
            # snapshot hash must match stored content_hash (tamper-evident)
            recomputed = _compute_content_hash(mv["summary_snapshot"])
            assert recomputed == mv["content_hash"], (
                f"memory {mv['memory_id']} snapshot 哈希不匹配 content_hash"
            )
    finally:
        await db.close()
