# tests/test_v06_migration.py
"""v0.6.0 数据库迁移测试"""
import asyncio
import aiosqlite
import pytest
from pathlib import Path

MIGRATION_SQL = Path("db/migrations/v06_cognitive.sql").read_text()

@pytest.fixture
async def migrated_db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        # 创建基础表
        schema = Path("db/schema.sql").read_text()
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
