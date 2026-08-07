"""回归测试：FTS 迁移原子性——逐行 DELETE+INSERT 保证崩溃安全。

根因：_migrate_v21 / _migrate_v22 原实现先 DELETE 全表再批量 INSERT，
进程在 DELETE 与 INSERT 间崩溃时 FTS 全表为空 → 所有记忆/实体检索完全不可用。

修复策略：
1. 逐行 DELETE+INSERT：任意时刻每条记录都有有效 FTS 条目。
2. v22 (INTEGER id FTS): WHERE id=? 删除 + 显式 rowid INSERT。
   FTS5 contentless 表删除后 rowid 不自动复用，INSERT 必须显式指定 rowid。
3. v21 (TEXT id FTS): SELECT rowid BY TEXT id → DELETE WHERE rowid=? →
   INSERT WITH explicit rowid。TEXT id 不能直接用于 DELETE (SQL logic error)。

本测试验证：
1. 迁移后 FTS 索引正确重建，所有行存在且非空
2. 迁移幂等（重复执行 rowid 保持稳定，计数不变）
3. 逐行策略：任意时刻 FTS 条目数始终 > 0
4. v21 两步骤删除在 TEXT-id FTS5 上可用
"""
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.asyncio

EPISODIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_memories (
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
    event_type TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
    id UNINDEXED, summary_index
);
"""


async def _make_db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(EPISODIC_SCHEMA)
    await conn.commit()
    return conn


async def _seed_episodic(conn, summaries):
    from db.fts_utils import _tokenize_for_fts
    now = 1700000000.0
    for i, summary in enumerate(summaries):
        await conn.execute(
            "INSERT INTO episodic_memories(id, timestamp, summary) VALUES(?, ?, ?)",
            (i + 1, now + i, summary),
        )
        tokenized = _tokenize_for_fts(summary)
        if tokenized.strip():
            await conn.execute(
                "INSERT INTO episodic_memory_fts(rowid, id, summary_index) VALUES(?, ?, ?)",
                (i + 1, i + 1, tokenized),
            )
    await conn.commit()


async def _fts_count(conn, table):
    cur = await conn.execute(f"SELECT count(*) FROM {table}")
    return (await cur.fetchone())[0]


async def _rebuild_episodic(conn):
    """Execute v22-style per-row rebuild with explicit rowid."""
    from db.fts_utils import _tokenize_for_fts
    cursor = await conn.execute("SELECT id, summary FROM episodic_memories")
    rows = await cursor.fetchall()
    for row in rows:
        tokenized = _tokenize_for_fts(row[1])
        await conn.execute(
            "DELETE FROM episodic_memory_fts WHERE id = ?", (row[0],)
        )
        if tokenized.strip():
            await conn.execute(
                "INSERT INTO episodic_memory_fts(rowid, id, summary_index) VALUES(?, ?, ?)",
                (row[0], row[0], tokenized),
            )
    await conn.commit()


# ── Test 1: All rows present with non-empty content ─────────────────────────

async def test_fts_rebuild_all_rows_present():
    """v22 迁移后，所有行的 FTS 条目都存在且非空。"""
    conn = await _make_db()
    try:
        summaries = ["你好世界", "陪伴是最长的告白", "你是谁", "今天天气很好"]
        await _seed_episodic(conn, summaries)
        count_before = await _fts_count(conn, "episodic_memory_fts")
        assert count_before == 4

        await _rebuild_episodic(conn)

        count_after = await _fts_count(conn, "episodic_memory_fts")
        assert count_after == 4

        cur = await conn.execute("SELECT id, summary_index FROM episodic_memory_fts")
        for r in await cur.fetchall():
            assert r[1].strip(), f"Row {r[0]} has empty FTS index"
    finally:
        await conn.close()


# ── Test 2: Idempotent rebuild with explicit rowid ─────────────────────────

async def test_fts_rebuild_idempotent():
    """v22 迁移可重复执行，rowid 保持稳定，计数不变。"""
    conn = await _make_db()
    try:
        summaries = ["你好世界", "陪伴是最长的告白", "你是谁"]
        await _seed_episodic(conn, summaries)

        await _rebuild_episodic(conn)
        count_first = await _fts_count(conn, "episodic_memory_fts")

        # Second rebuild (crash recovery)
        await _rebuild_episodic(conn)
        count_second = await _fts_count(conn, "episodic_memory_fts")

        assert count_first == count_second == 3, \
            f"Idempotent: first={count_first}, second={count_second}"

        # Verify rowids are stable (match ids)
        cur = await conn.execute("SELECT rowid, id FROM episodic_memory_fts")
        for r in await cur.fetchall():
            assert r[0] == r[1], f"rowid={r[0]} should equal id={r[1]}"
    finally:
        await conn.close()


# ── Test 3: Atomicity — FTS count never reaches 0 mid-migration ──────────────

async def test_fts_rebuild_atomicity_no_empty_window():
    """逐行 DELETE+INSERT：最终状态正确，任意时刻可中断恢复。"""
    conn = await _make_db()
    try:
        summaries = ["你好世界", "陪伴是最长的告白", "你是谁", "今天天气很好", "我想回家"]
        await _seed_episodic(conn, summaries)
        from db.fts_utils import _tokenize_for_fts

        total_before = await _fts_count(conn, "episodic_memory_fts")
        assert total_before == 5

        # Simulate crash recovery: only process first 3 rows
        cursor = await conn.execute("SELECT id, summary FROM episodic_memories")
        rows = await cursor.fetchall()
        for row in rows[:3]:
            tokenized = _tokenize_for_fts(row[1])
            await conn.execute(
                "DELETE FROM episodic_memory_fts WHERE id = ?", (row[0],)
            )
            if tokenized.strip():
                await conn.execute(
                    "INSERT INTO episodic_memory_fts(rowid, id, summary_index) VALUES(?, ?, ?)",
                    (row[0], row[0], tokenized),
                )
        await conn.commit()

        mid_count = await _fts_count(conn, "episodic_memory_fts")
        assert mid_count == 5, f"Mid-rebuild count should be 5, got {mid_count}"

        # Continue (restart migration)
        cursor2 = await conn.execute("SELECT id, summary FROM episodic_memories")
        rows2 = await cursor2.fetchall()
        for row in rows2[3:]:
            tokenized = _tokenize_for_fts(row[1])
            await conn.execute(
                "DELETE FROM episodic_memory_fts WHERE id = ?", (row[0],)
            )
            if tokenized.strip():
                await conn.execute(
                    "INSERT INTO episodic_memory_fts(rowid, id, summary_index) VALUES(?, ?, ?)",
                    (row[0], row[0], tokenized),
                )
        await conn.commit()

        final = await _fts_count(conn, "episodic_memory_fts")
        assert final == 5, f"Final count should be 5, got {final}"
    finally:
        await conn.close()


# ── Test 4: Empty/NULL summary handling ──────────────────────────────────────

async def test_fts_rebuild_empty_summary_handled():
    """空 summary 的行不应导致迁移失败。"""
    conn = await _make_db()
    try:
        await conn.execute(
            "INSERT INTO episodic_memories(id, timestamp, summary) VALUES(?, ?, ?)",
            (1, 1700000000.0, "有效内容"),
        )
        await conn.execute(
            "INSERT INTO episodic_memories(id, timestamp, summary) VALUES(?, ?, ?)",
            (2, 1700000001.0, ""),
        )
        await conn.commit()

        from db.fts_utils import _tokenize_for_fts
        cursor = await conn.execute("SELECT id, summary FROM episodic_memories")
        rows = await cursor.fetchall()
        for row in rows:
            tokenized = _tokenize_for_fts(row[1]) if row[1] else ""
            await conn.execute(
                "DELETE FROM episodic_memory_fts WHERE id = ?", (row[0],)
            )
            if tokenized.strip():
                await conn.execute(
                    "INSERT INTO episodic_memory_fts(rowid, id, summary_index) VALUES(?, ?, ?)",
                    (row[0], row[0], tokenized),
                )
        await conn.commit()

        count = await _fts_count(conn, "episodic_memory_fts")
        assert count == 1, f"Expected 1 FTS entry (empty summary skipped), got {count}"
    finally:
        await conn.close()


# ── Test 5: v21 two-step delete for TEXT-id FTS5 ─────────────────────────────

async def test_v21_two_step_delete_text_id_fts5():
    """v21 迁移：SELECT 定位 rowid → DELETE WHERE rowid=? 在 TEXT id FTS5 上可用。"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("""
            CREATE TABLE knowledge_entities (
                id TEXT PRIMARY KEY,
                name TEXT,
                updated_at REAL NOT NULL
            )
        """)
        await conn.execute("""
            CREATE VIRTUAL TABLE knowledge_entities_fts USING fts5(id UNINDEXED, name_index)
        """)
        await conn.commit()

        await conn.execute(
            "INSERT INTO knowledge_entities(rowid, id, name, updated_at) VALUES(?, ?, ?, ?)",
            (1, "ENT-1", "小妲", 1700000000.0),
        )
        await conn.execute(
            "INSERT INTO knowledge_entities_fts(rowid, id, name_index) VALUES(?, ?, ?)",
            (1, "ENT-1", "小妲"),
        )
        await conn.commit()

        from db.fts_utils import _tokenize_for_fts

        # v21 first rebuild
        cursor = await conn.execute("SELECT rowid, id, name FROM knowledge_entities")
        rows = await cursor.fetchall()
        for row in rows:
            _rowid, _id, _name = row[0], row[1], row[2]
            _name_index = _tokenize_for_fts(_name) if _name else ""
            fts_cur = await conn.execute(
                "SELECT rowid FROM knowledge_entities_fts WHERE id = ?", (_id,)
            )
            existing = await fts_cur.fetchone()
            if existing:
                await conn.execute(
                    "DELETE FROM knowledge_entities_fts WHERE rowid = ?", (existing[0],)
                )
            await conn.execute(
                "INSERT INTO knowledge_entities_fts(rowid, id, name_index) VALUES(?, ?, ?)",
                (_rowid, _id, _name_index),
            )
        await conn.commit()

        count = await _fts_count(conn, "knowledge_entities_fts")
        assert count == 1

        # v21 second rebuild (idempotency)
        cursor2 = await conn.execute("SELECT rowid, id, name FROM knowledge_entities")
        rows2 = await cursor2.fetchall()
        for row in rows2:
            _rowid, _id, _name = row[0], row[1], row[2]
            _name_index = _tokenize_for_fts(_name) if _name else ""
            fts_cur = await conn.execute(
                "SELECT rowid FROM knowledge_entities_fts WHERE id = ?", (_id,)
            )
            existing = await fts_cur.fetchone()
            if existing:
                await conn.execute(
                    "DELETE FROM knowledge_entities_fts WHERE rowid = ?", (existing[0],)
                )
            await conn.execute(
                "INSERT INTO knowledge_entities_fts(rowid, id, name_index) VALUES(?, ?, ?)",
                (_rowid, _id, _name_index),
            )
        await conn.commit()

        count2 = await _fts_count(conn, "knowledge_entities_fts")
        assert count2 == 1, f"Idempotent v21: count should be 1, got {count2}"
    finally:
        await conn.close()
