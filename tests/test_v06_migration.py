"""v0.6.0 认知架构数据库迁移测试（v14 迁移的表结构校验）。

依据背景：原测试直接执行 db/migrations/v06_cognitive.sql（v14 迁移的
SQL 归档版）。该 SQL 与 Python 实现 _migrate_v14_cognitive_tables 完全
等价，属双轨维护，SQL 文件已删除。本测试改为走真实迁移链
（DatabaseManager 全量迁移到 v27，链上必然经过 v14），断言 5 张认知表、
episodic_memories 新增 3 列与 9 个索引的形态，防认知架构回归。
"""
import aiosqlite
import pytest

from db.database import CURRENT_SCHEMA_VERSION, DatabaseManager

MAJOR_TABLE = "semantic_memories"


@pytest.fixture
async def migrated_db(tmp_path):
    """构造全新数据库并全量迁移到最新版本（链上包含 v14）。"""
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


async def test_semantic_memories_table(migrated_db):
    columns = await _columns(migrated_db, "semantic_memories")
    assert {"cluster_id", "salience", "emotion_label"} <= columns


async def test_memory_connections_table(migrated_db):
    columns = await _columns(migrated_db, "memory_connections")
    assert {"source_id", "target_id", "weight", "edge_type"} <= columns


async def test_bridge_memories_table(migrated_db):
    columns = await _columns(migrated_db, "bridge_memories")
    assert {"cross_session", "discovery_reason"} <= columns


async def test_episodic_memories_new_columns(migrated_db):
    columns = await _columns(migrated_db, "episodic_memories")
    assert {"salience", "last_accessed", "status"} <= columns


async def test_memory_revisions_table(migrated_db):
    columns = await _columns(migrated_db, "memory_revisions")
    assert {"old_memory_id", "new_memory_id", "conflict_type", "revision_chain"} <= columns


async def test_preference_patterns_table(migrated_db):
    columns = await _columns(migrated_db, "preference_patterns")
    assert {"pattern_text", "confidence", "salience", "match_count"} <= columns


async def test_indexes_exist(migrated_db):
    """验证认知 9 个索引已创建。"""
    expected = {
        "idx_semantic_cluster",
        "idx_semantic_salience",
        "idx_conn_source",
        "idx_conn_target",
        "idx_conn_type",
        "idx_bridge_source",
        "idx_bridge_target",
        "idx_revisions_old",
        "idx_preference_salience",
    }
    rows = await migrated_db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    actual = {row["name"] for row in rows}
    assert not (expected - actual), f"missing indexes: {expected - actual}"