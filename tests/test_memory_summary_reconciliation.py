from __future__ import annotations

import aiosqlite
import pytest

from db.db_memory import MemoryDB


@pytest.fixture
async def summary_db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(
        """
        CREATE TABLE memory_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary_text TEXT NOT NULL,
            created_at REAL NOT NULL,
            memory_count INTEGER DEFAULT 0
        );
        CREATE TABLE memory_reconciliation_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE,
            proposed_action TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            executed INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    await conn.commit()
    db = MemoryDB(conn)
    yield db
    await conn.close()


@pytest.mark.asyncio
async def test_shadow_proposals_do_not_hide_existing_summaries(summary_db):
    await summary_db._conn.executemany(
        "INSERT INTO memory_summaries(summary_text, created_at, memory_count) VALUES (?, ?, 1)",
        [("older", 10.0), ("newer", 30.0)],
    )
    await summary_db._conn.execute(
        "INSERT INTO memory_reconciliation_actions"
        "(job_id, proposed_action, decision_json, executed, created_at) "
        "VALUES (1, 'merge', '{}', 0, 20.0)"
    )
    await summary_db._conn.commit()

    rows = await summary_db.get_memory_summaries(limit=5)

    assert [row["summary_text"] for row in rows] == ["newer", "older"]


@pytest.mark.asyncio
async def test_executed_action_filters_stale_aggregate_summaries(summary_db):
    await summary_db._conn.executemany(
        "INSERT INTO memory_summaries(summary_text, created_at, memory_count) VALUES (?, ?, 1)",
        [("contains superseded fact", 10.0), ("rebuilt after action", 30.0)],
    )
    await summary_db._conn.execute(
        "INSERT INTO memory_reconciliation_actions"
        "(job_id, proposed_action, decision_json, executed, created_at) "
        "VALUES (1, 'merge', '{}', 1, 20.0)"
    )
    await summary_db._conn.commit()

    rows = await summary_db.get_memory_summaries(limit=5)

    assert [row["summary_text"] for row in rows] == ["rebuilt after action"]


@pytest.mark.asyncio
async def test_legacy_database_without_reconciliation_table_keeps_summaries():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(
        "CREATE TABLE memory_summaries ("
        "id INTEGER PRIMARY KEY, summary_text TEXT, created_at REAL, memory_count INTEGER)"
    )
    await conn.execute(
        "INSERT INTO memory_summaries VALUES (1, 'legacy', 10.0, 1)"
    )
    await conn.commit()
    try:
        rows = await MemoryDB(conn).get_memory_summaries(limit=5)
        assert [row["summary_text"] for row in rows] == ["legacy"]
    finally:
        await conn.close()
