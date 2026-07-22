"""DB Migration v13 测试：验证 mem0 SPEC 优化新增的列、表、索引存在"""

import pytest


@pytest.fixture
async def migrated_db(tmp_path):
    """创建并迁移到 v13 的数据库"""
    db_path = tmp_path / "test_v13.db"
    from db.database import DatabaseManager
    db = DatabaseManager(db_path)
    await db.init()
    yield db
    await db.close()


class TestMigrationV13:
    """验证 v13 迁移：episodic_memories 新增3字段 + 3新表 + 索引"""

    async def test_episodic_memories_has_user_id_column(self, migrated_db):
        """验证 episodic_memories 表有 user_id 列，默认 'default'"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "user_id" in columns

    async def test_episodic_memories_has_agent_id_column(self, migrated_db):
        """验证 episodic_memories 表有 agent_id 列，默认 'xiaoda'"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "agent_id" in columns

    async def test_episodic_memories_has_is_raw_column(self, migrated_db):
        """验证 episodic_memories 表有 is_raw 列，默认 0"""
        cursor = await migrated_db._conn.execute("PRAGMA table_info(episodic_memories)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "is_raw" in columns

    async def test_memory_entities_table_exists(self, migrated_db):
        """验证 memory_entities 表存在且有正确字段"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entities'"
        )
        row = await cursor.fetchone()
        assert row is not None
        # 验证字段
        cursor = await migrated_db._conn.execute("PRAGMA table_info(memory_entities)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "name" in cols
        assert "entity_type" in cols
        assert "kind" in cols
        assert "observations" in cols
        assert "memory_count" in cols
        assert "first_seen" in cols
        assert "last_seen" in cols
        assert "metadata_json" in cols

    async def test_memory_entities_fts_table_exists(self, migrated_db):
        """验证 memory_entities_fts 虚拟表存在"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entities_fts'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_entity_memory_links_table_exists(self, migrated_db):
        """验证 entity_memory_links 表存在且有正确字段"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_memory_links'"
        )
        row = await cursor.fetchone()
        assert row is not None
        cursor = await migrated_db._conn.execute("PRAGMA table_info(entity_memory_links)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "entity_id" in cols
        assert "memory_id" in cols
        assert "confidence" in cols
        assert "created_at" in cols

    async def test_episodic_scope_index_exists(self, migrated_db):
        """验证 idx_episodic_scope 复合索引存在"""
        cursor = await migrated_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_episodic_scope'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_schema_version_is_13(self, migrated_db):
        """验证 schema_version 表记录了 v13"""
        cursor = await migrated_db._conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        assert row[0] >= 13

    async def test_existing_memories_backfill_is_raw(self, migrated_db):
        """验证现有记忆被回填 is_raw=0, user_id='default', agent_id='xiaoda'"""
        # 先插入一条记忆（模拟旧数据，不传新字段）
        import time
        await migrated_db._conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
            (time.time(), "测试旧记忆"),
        )
        await migrated_db._conn.commit()
        # 查询验证默认值
        cursor = await migrated_db._conn.execute(
            "SELECT user_id, agent_id, is_raw FROM episodic_memories WHERE summary='测试旧记忆'"
        )
        row = await cursor.fetchone()
        assert row["user_id"] == "default"
        assert row["agent_id"] == "xiaoda"
        assert row["is_raw"] == 0
