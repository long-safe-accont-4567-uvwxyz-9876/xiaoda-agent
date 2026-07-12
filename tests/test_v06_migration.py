# tests/test_v06_migration.py
"""v0.6.0 数据库迁移测试"""
import asyncio
import aiosqlite
import pytest
from pathlib import Path

MIGRATION_SQL = Path("db/migrations/v06_cognitive.sql").read_text(encoding="utf-8")

@pytest.fixture
async def migrated_db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        # 创建基础表
        schema = Path("db/schema.sql").read_text(encoding="utf-8")
        await db.executescript(schema)
        # 执行迁移
        await db.executescript(MIGRATION_SQL)
        await db.commit()
    return db_path

async def test_semantic_memories_table(migrated_db):
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute("PRAGMA table_info(semantic_memories)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "cluster_id" in columns
        assert "salience" in columns
        assert "emotion_label" in columns

async def test_memory_connections_table(migrated_db):
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute("PRAGMA table_info(memory_connections)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "source_id" in columns
        assert "target_id" in columns
        assert "weight" in columns
        assert "edge_type" in columns

async def test_bridge_memories_table(migrated_db):
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute("PRAGMA table_info(bridge_memories)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "cross_session" in columns
        assert "discovery_reason" in columns

async def test_episodic_memories_new_columns(migrated_db):
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute("PRAGMA table_info(episodic_memories)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "salience" in columns
        assert "last_accessed" in columns
        assert "status" in columns

async def test_memory_revisions_table(migrated_db):
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute("PRAGMA table_info(memory_revisions)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "old_memory_id" in columns
        assert "new_memory_id" in columns
        assert "conflict_type" in columns
        assert "revision_chain" in columns

async def test_preference_patterns_table(migrated_db):
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute("PRAGMA table_info(preference_patterns)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "pattern_text" in columns
        assert "confidence" in columns
        assert "salience" in columns
        assert "match_count" in columns

async def test_indexes_exist(migrated_db):
    """验证 9 个索引中至少代表性的几个已创建。"""
    expected_indexes = {
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
    async with aiosqlite.connect(migrated_db) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        actual = {row[0] for row in await cursor.fetchall()}
    missing = expected_indexes - actual
    assert not missing, f"missing indexes: {missing}"