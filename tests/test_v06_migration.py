"""v0.6.0 认知架构数据库迁移测试（v14 迁移的表结构校验）。

依据背景：原测试直接执行 db/migrations/v06_cognitive.sql（v14 迁移的
SQL 归档版）。该 SQL 与 Python 实现 _migrate_v14_cognitive_tables 完全
等价，属双轨维护，SQL 文件已删除。本测试改为走真实迁移链
（DatabaseManager 全量迁移到最新版本，链上必然经过 v14），断言
episodic_memories 新增 3 列与存留表 memory_revisions 的形态。

v30 已 DROP 四张零读写认知表（semantic_memories/memory_connections/
bridge_memories/preference_patterns），本测试同时断言它们确已不存在，
防误复活。
"""
import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager


@pytest.fixture
async def migrated_db(tmp_path):
    """构造全新数据库并全量迁移到最新版本（链上包含 v14 与 v30）。"""
    db_path = tmp_path / "v06.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    row = await manager.fetch_one("SELECT MAX(version) AS version FROM schema_version")
    assert row["version"] == CURRENT_SCHEMA_VERSION
    yield manager
    await manager.close()


async def _columns(manager: DatabaseManager, table: str) -> set[str]:
    rows = await manager.fetch_all(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}


async def test_episodic_memories_new_columns(migrated_db):
    columns = await _columns(migrated_db, "episodic_memories")
    assert {"salience", "last_accessed", "status"} <= columns


async def test_memory_revisions_table(migrated_db):
    columns = await _columns(migrated_db, "memory_revisions")
    assert {"old_memory_id", "new_memory_id", "conflict_type", "revision_chain"} <= columns


async def test_dead_v06_cognitive_tables_dropped(migrated_db):
    """v30 应已清除四张零读写认知表。"""
    for table in ("semantic_memories", "memory_connections",
                  "bridge_memories", "preference_patterns"):
        columns = await _columns(migrated_db, table)
        assert columns == set(), f"table {table} should be dropped, got columns={columns}"


async def test_surviving_indexes_exist(migrated_db):
    """验证 v14 索引中未被 v30 连带清除的部分仍存在（memory_revisions）。"""
    expected = {
        "idx_revisions_old",
    }
    rows = await migrated_db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    actual = {row["name"] for row in rows}
    assert not (expected - actual), f"missing indexes: {expected - actual}"
