"""purge_transient_learnings 必须无条件结束写事务。

回归背景（2026-11-09 启动清理 database is locked）：
nudge_engine._tick 先调 _check_auto_promote → learning_manager.auto_promote
→ purge_transient_learnings 执行 11 条 DELETE；一行都没删中时旧实现不 commit，
共享主连接就挂着一个未提交写事务；紧接着 _check_data_cleanup 用独立连接
DELETE audit_logs / api_usage / sessions，各等满 20s 后失败（共 60.2s）。
"""
import time

import aiosqlite

from db.db_learning import LearningDB

_SCHEMA = """
CREATE TABLE learnings (
    learning_id TEXT PRIMARY KEY,
    category TEXT,
    status TEXT,
    summary TEXT,
    pattern_key TEXT,
    last_seen REAL
)
"""


async def _open(tmp_path):
    path = tmp_path / "agent.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_SCHEMA)
    await conn.commit()
    return path, conn


async def test_purge_closes_write_transaction_when_nothing_deleted(tmp_path):
    path, conn = await _open(tmp_path)
    try:
        result = await LearningDB(conn).purge_transient_learnings()
        assert result == {"transient": 0, "stale": 0}
        assert conn.in_transaction is False, "未删到行也必须结束写事务"
    finally:
        await conn.close()


async def test_other_connection_can_write_right_after_purge(tmp_path):
    """复现启动现场：purge 后另一条连接立即写，不能撞 database is locked。"""
    path, conn = await _open(tmp_path)
    other = None
    try:
        await LearningDB(conn).purge_transient_learnings()
        other = await aiosqlite.connect(str(path))
        await other.execute("PRAGMA busy_timeout=500")
        await other.execute(
            "INSERT INTO learnings "
            "(learning_id, category, status, summary, pattern_key, last_seen) "
            "VALUES ('L1', 'error_pattern', 'pending', 'timeout', 'p1', ?)",
            (time.time(),),
        )
        await other.commit()
        row = await (await other.execute("SELECT COUNT(*) FROM learnings")).fetchone()
        assert row[0] == 1
    finally:
        if other is not None:
            await other.close()
        await conn.close()


async def test_purge_still_deletes_and_commits_when_rows_match(tmp_path):
    path, conn = await _open(tmp_path)
    try:
        now = time.time()
        await conn.execute(
            "INSERT INTO learnings VALUES "
            "('L1', 'error_pattern', 'pending', 'TimeoutError: timed out', 'p1', ?)",
            (now,),
        )
        await conn.execute(
            "INSERT INTO learnings VALUES "
            "('L2', 'error_pattern', 'pending', 'keep me', 'p2', ?)",
            (now,),
        )
        await conn.commit()
        result = await LearningDB(conn).purge_transient_learnings()
        assert result["transient"] >= 1
        assert conn.in_transaction is False
        left = await (await conn.execute(
            "SELECT learning_id FROM learnings ORDER BY learning_id")).fetchall()
        assert [r[0] for r in left] == ["L2"]
    finally:
        await conn.close()
