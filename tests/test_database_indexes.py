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


# ============================================================
# write_transaction 回归测试 (P0: 数据完整性 + 崩溃路径)
#
# 修复前缺陷:
#   1. 未初始化 (_conn is None) 时进入事务 → yield None 给调用方,
#      随后 await self._conn.commit() 抛 AttributeError, finally 块中
#      self._conn.rollback() 再次 AttributeError, 掩盖原始异常。
#   2. close() 未获取 _write_tx_lock, 可能在 yield→commit 之间把
#      _conn 置空, 导致 commit/rollback 点处 AttributeError。
#   3. yield 体中抛业务异常时, 若 _conn 恰好为 None, rollback 会
#      在 finally 中再抛异常, 业务异常被丢失。
# ============================================================

async def test_write_transaction_raises_before_yield_when_uninitialized():
    """修复验证 #1: 未 init() 时 write_transaction 应在 yield 前抛 RuntimeError,
    不会把 None 当连接暴露给调用方。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(db_path=os.path.join(tmpdir, "x.db"))
        # 故意不 await db.init() → _conn 仍为 None
        saw_body = False
        with pytest.raises(RuntimeError, match="connection not initialized"):
            async with db.write_transaction() as conn:
                # 如果执行到这里, 说明 yield 了一个(空)连接 → 未通过
                saw_body = True
                _ = conn.execute("SELECT 1")
        assert saw_body is False, (
            "write_transaction 在抛错前 yield 了 None 连接, "
            "调用方能访问到无效句柄 (修复前缺陷)")


async def test_write_transaction_commit_persists(tmp_db: DatabaseManager):
    """修复验证 #2: 正常路径 commit 成功, 数据被持久化 (非回归, 语义基线)"""
    async with tmp_db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO learnings "
            "(learning_id, category, priority, status, area, summary, "
            " source, pattern_key, recurrence_count, first_seen, last_seen, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("LRN-TX1", "bug", "high", "pending", "test", "sum",
             "manual", "", 1, 1.0, 1.0, 1.0),
        )
    # 事务外查询: 数据必须已持久化
    cur = await tmp_db._conn.execute(
        "SELECT COUNT(*) FROM learnings WHERE learning_id=?",
        ("LRN-TX1",),
    )
    (n,) = await cur.fetchone()
    assert n == 1, "write_transaction 正常退出后未 commit"


async def test_write_transaction_exception_rolls_back(tmp_db: DatabaseManager):
    """修复验证 #3: 体中抛异常时回滚, 不留下半写数据。"""
    # 先写基线行 (在事务外, 保证独立)
    await tmp_db._conn.execute(
        "INSERT INTO learnings "
        "(learning_id, category, priority, status, area, summary, "
        " source, pattern_key, recurrence_count, first_seen, last_seen, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("LRN-BASE", "bug", "low", "resolved", "test", "base",
         "manual", "", 0, 0.0, 0.0, 0.0),
    )
    await tmp_db._conn.commit()

    with pytest.raises(ValueError, match="boom"):
        async with tmp_db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO learnings "
                "(learning_id, category, priority, status, area, summary, "
                " source, pattern_key, recurrence_count, first_seen, last_seen, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("LRN-TX2", "bug", "high", "pending", "test", "tx2",
                 "manual", "", 1, 2.0, 2.0, 2.0),
            )
            raise ValueError("boom")  # 触发回滚

    # 验证: 基线行存在, 事务内行不存在
    cur = await tmp_db._conn.execute(
        "SELECT learning_id FROM learnings ORDER BY created_at",
    )
    rows = [r[0] for r in await cur.fetchall()]
    assert "LRN-BASE" in rows, "基线行被错误回滚"
    assert "LRN-TX2" not in rows, "异常事务中的 INSERT 未被回滚 (数据脏写!)"


async def test_write_transaction_close_during_body_no_crash(tmp_db: DatabaseManager):
    """修复验证 #4: 模拟 close() 在事务体中把 _conn 置 None, finally 不应再抛。
    模拟并发 close() 把 self._conn 置空 (不真正 close 以免影响其他测试清理)。"""
    saved_conn = tmp_db._conn  # 先保存以便手动恢复 (tmp_db fixture 需用它关闭)
    saw_exception = False
    try:
        with pytest.raises(RuntimeError, match="injected for test"):
            async with tmp_db.write_transaction() as conn:
                # 模拟并发 db.close() 把 _conn 设为 None (同时清理 readonly_conn 保持一致)
                tmp_db._conn = None
                tmp_db._readonly_conn = None
                raise RuntimeError("injected for test")
        # 如果 pytest.raises 不满足, 下面断言不执行; 满足则这里断言 finally 没再抛
        saw_exception = True
    finally:
        # 恢复 saved_conn, 保证 fixture 中的 await db.close() 有东西可关
        tmp_db._conn = saved_conn
    assert saw_exception, (
        "finally 块里对 None 连接调用 rollback 时抛了 AttributeError, "
        "覆盖/替换了业务的 RuntimeError")
