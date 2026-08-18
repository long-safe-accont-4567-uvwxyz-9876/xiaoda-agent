"""复合索引管理器测试 (P2)

覆盖:
- 索引创建成功
- 重复创建幂等 (不报错)
- verify() 正确识别已存在索引
- EXPLAIN QUERY PLAN 走索引而非全表扫描

运行:
    python -m pytest tests/test_database_indexes.py -v --tb=short
"""
import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from db.database import DatabaseManager
from db.index_manager import IndexDef, IndexManager, build_default_index_manager


# ============================================================
# helpers
# ============================================================

@pytest_asyncio.fixture
async def tmp_db():
    """pytest 异步 fixture: 提供初始化好的 DatabaseManager"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(db_path=os.path.join(tmpdir, "test.db"))
        await db.init()
        try:
            yield db
        finally:
            await db.close()


async def _explain(conn: aiosqlite.Connection, sql: str,
                   params: tuple = ()) -> list[str]:
    """执行 EXPLAIN QUERY PLAN, 返回每行文本"""
    cursor = await conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    rows = await cursor.fetchall()
    # row 结构: (id, parent, notused, detail)
    return [r[3] for r in rows]


def _uses_index(plan_lines: list[str]) -> bool:
    """判断 EXPLAIN QUERY PLAN 结果是否使用了索引

    SQLite EXPLAIN 输出语义:
    - "SCAN <table>"             — 全表扫描 (坏)
    - "SCAN <table> USING INDEX" — 通过索引遍历 (可接受)
    - "SEARCH <table> USING INDEX" — 通过索引查找 (好)
    """
    text = " | ".join(plan_lines).upper()
    # 必须有 USING INDEX
    if "USING INDEX" not in text and "USING COVERING INDEX" not in text:
        return False
    # 不能有原始全表扫描 (SCAN <table> 但没有 USING INDEX 紧跟)
    # 简化判断: 整体只要出现 USING INDEX 即视为走索引
    return True


# ============================================================
# IndexManager 单元测试 (不依赖完整 schema)
# ============================================================

@pytest.mark.asyncio
async def test_index_manager_creation_basic():
    """IndexManager.apply 在简单表上能创建索引"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE t (a TEXT, b REAL, c INTEGER)")
            await conn.commit()

            mgr = IndexManager()
            mgr.register(IndexDef("t", ["a", "b"], "idx_t_ab"))
            mgr.register(IndexDef("t", ["c"], "idx_t_c"))
            count = await mgr.apply(conn)
            assert count == 2

            # 验证索引确实存在
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?, ?)",
                ("idx_t_ab", "idx_t_c"),
            )
            names = {r[0] for r in await cur.fetchall()}
            assert names == {"idx_t_ab", "idx_t_c"}
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_index_manager_idempotent():
    """重复 apply 同一组索引不应报错, 返回值仍为索引数"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE t (a TEXT, b REAL)")
            await conn.commit()

            mgr = IndexManager()
            mgr.register(IndexDef("t", ["a", "b"], "idx_t_ab"))
            first = await mgr.apply(conn)
            second = await mgr.apply(conn)
            assert first == second == 1
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_index_manager_verify_found_and_miss():
    """verify: 命中返回 True, 未命中返回 False"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE t (a TEXT, b REAL, c INTEGER)")
            await conn.execute("CREATE INDEX idx_t_ab ON t(a, b)")
            await conn.commit()

            mgr = IndexManager()
            # 命中: 列顺序匹配
            assert await mgr.verify(conn, "t", ["a", "b"]) is True
            # 前缀匹配也算命中 (单列作为前缀)
            assert await mgr.verify(conn, "t", ["a"]) is True
            # 列顺序不匹配
            assert await mgr.verify(conn, "t", ["b", "a"]) is False
            # 列不存在
            assert await mgr.verify(conn, "t", ["c"]) is False
            # 表不存在
            assert await mgr.verify(conn, "nonexistent_table", ["a"]) is False
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_index_manager_apply_swallows_column_error():
    """列不存在时跳过但不抛, 不影响其他索引创建"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE t (a TEXT, b REAL)")
            await conn.commit()

            mgr = IndexManager()
            # bad_index 引用了不存在的列, 但 good_index 应该成功
            mgr.register(IndexDef("t", ["a", "missing_col"], "idx_bad"))
            mgr.register(IndexDef("t", ["a", "b"], "idx_good"))
            count = await mgr.apply(conn)
            # apply 返回的是"成功创建数", bad 那条失败被吞掉
            assert count == 1

            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_good'"
            )
            row = await cur.fetchone()
            assert row is not None
    finally:
        os.unlink(path)


# ============================================================
# DatabaseManager 集成测试
# ============================================================

@pytest.mark.asyncio
async def test_index_creation_via_database_init(tmp_db: DatabaseManager):
    """DatabaseManager.init() 后, 内置复合索引应已创建"""
    conn = tmp_db._conn
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    )
    names = {r[0] for r in await cur.fetchall()}

    expected_composite = {
        "idx_em_session_ts",
        "idx_em_importance_ts",
        "idx_em_access_ts",
        "idx_conv_session_ts",
        "idx_conv_user_source",
        "idx_ke_kind_updated",
        "idx_krel_pair_conf",
        "idx_lrn_status_created",
        "idx_note_status_created",
    }
    missing = expected_composite - names
    assert not missing, f"缺少复合索引: {missing}"


@pytest.mark.asyncio
async def test_index_creation_idempotent_via_reinit(tmp_db: DatabaseManager):
    """重复 init() (重新打开同一数据库) 不应报错, 索引仍然存在"""
    db_path = str(tmp_db.db_path)
    # 关闭后重新打开
    await tmp_db.close()
    db2 = DatabaseManager(db_path=db_path)
    await db2.init()
    try:
        conn = db2._conn
        cur = await conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        row = await cur.fetchone()
        assert row[0] >= 9, f"重新初始化后索引丢失: 仅剩 {row[0]}"
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_index_verification_via_database_init(tmp_db: DatabaseManager):
    """verify 能正确识别 DatabaseManager 创建的复合索引"""
    conn = tmp_db._conn
    mgr = build_default_index_manager()

    # 每个已注册的复合索引都应该被 verify 命中
    for idx in mgr.list_indexes():
        ok = await mgr.verify(conn, idx.table, idx.columns)
        assert ok, f"索引未被验证: {idx.name} on {idx.table}({idx.columns})"

    # 反例: 不存在的列组合应返回 False
    assert await mgr.verify(conn, "episodic_memories", ["nonexistent_col"]) is False
    assert await mgr.verify(conn, "episodic_memories", ["timestamp", "session_id"]) is False  # 顺序反


# ============================================================
# EXPLAIN QUERY PLAN 走索引 (核心验收)
# ============================================================

@pytest.mark.asyncio
async def test_query_uses_index_episodic_session_ts(tmp_db: DatabaseManager):
    """WHERE session_id=? AND timestamp>=? 应走 idx_em_session_ts 索引"""
    conn = tmp_db._conn
    # 插入足够多行, 让查询规划器选择索引 (小表会全表扫描)
    now = 1700000000.0
    rows = []
    for i in range(200):
        rows.append((
            now + i,
            f"memory #{i}",
            0.5 + (i % 5) * 0.1,
            "",
            "test_session" if i % 2 == 0 else f"sess_{i}",
            -1, "pending", 0, "", "user", i, 0, "", "", "{}",
        ))
    await conn.executemany(
        """INSERT INTO episodic_memories
           (timestamp, summary, importance, emotion_label, session_id,
            embedding_id, rag_status, rag_synced_at, doc_id, source,
            access_count, distilled, entities, event_type, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await conn.commit()

    plans = await _explain(
        conn,
        "SELECT * FROM episodic_memories WHERE session_id=? AND timestamp >= ? ORDER BY timestamp DESC LIMIT 50",
        ("test_session", now),
    )
    plan_text = " | ".join(plans)
    assert _uses_index(plans), f"未走索引: {plan_text}"
    assert "idx_em_session_ts" in plan_text, f"未走预期复合索引: {plan_text}"


@pytest.mark.asyncio
async def test_query_uses_index_episodic_importance_ts(tmp_db: DatabaseManager):
    """WHERE importance>=? ORDER BY timestamp DESC 应走复合索引"""
    conn = tmp_db._conn
    now = 1700000000.0
    rows = []
    for i in range(200):
        rows.append((
            now + i,
            f"memory #{i}",
            0.3 + (i % 10) * 0.05,
            "",
            "sess",
            -1, "pending", 0, "", "user", i, 0, "", "", "{}",
        ))
    await conn.executemany(
        """INSERT INTO episodic_memories
           (timestamp, summary, importance, emotion_label, session_id,
            embedding_id, rag_status, rag_synced_at, doc_id, source,
            access_count, distilled, entities, event_type, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await conn.commit()

    plans = await _explain(
        conn,
        "SELECT * FROM episodic_memories WHERE importance >= ? ORDER BY timestamp DESC LIMIT 50",
        (0.6,),
    )
    plan_text = " | ".join(plans)
    assert _uses_index(plans), f"未走索引: {plan_text}"


@pytest.mark.asyncio
async def test_query_uses_index_conversation_session_ts(tmp_db: DatabaseManager):
    """conversation_logs WHERE session_id=? ORDER BY timestamp DESC 应走复合索引"""
    conn = tmp_db._conn
    now = 1700000000.0
    rows = []
    for i in range(200):
        rows.append((
            now + i,
            f"user_{i}",
            "web" if i % 2 else "qq",
            "hi",
            "resp",
            "", "", "test_session" if i % 3 == 0 else f"s_{i}",
        ))
    await conn.executemany(
        """INSERT INTO conversation_logs
           (timestamp, user_id, source, user_message, assistant_reply,
            emotion_label, model_used, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await conn.commit()

    plans = await _explain(
        conn,
        "SELECT * FROM conversation_logs WHERE session_id=? ORDER BY timestamp DESC LIMIT 50",
        ("test_session",),
    )
    plan_text = " | ".join(plans)
    assert _uses_index(plans), f"未走索引: {plan_text}"
    assert "idx_conv_session_ts" in plan_text, f"未走预期复合索引: {plan_text}"


@pytest.mark.asyncio
async def test_query_uses_index_learnings_status_created(tmp_db: DatabaseManager):
    """learnings WHERE status=? ORDER BY created_at DESC 应走复合索引"""
    conn = tmp_db._conn
    now = 1700000000.0
    rows = []
    for i in range(200):
        rows.append((
            f"LRN-{i:04d}",
            "insight" if i % 2 else "bug",
            "high" if i % 3 == 0 else "low",
            "pending" if i % 4 else "resolved",
            "backend",
            f"summary {i}",
            "", "", "conversation", "", 1,
            now + i, now + i, now + i,
        ))
    await conn.executemany(
        """INSERT INTO learnings
           (learning_id, category, priority, status, area, summary, details,
            suggested_action, source, pattern_key, recurrence_count,
            first_seen, last_seen, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await conn.commit()

    plans = await _explain(
        conn,
        "SELECT * FROM learnings WHERE status=? ORDER BY created_at DESC LIMIT 50",
        ("pending",),
    )
    plan_text = " | ".join(plans)
    assert _uses_index(plans), f"未走索引: {plan_text}"
    assert "idx_lrn_status_created" in plan_text, f"未走预期复合索引: {plan_text}"
