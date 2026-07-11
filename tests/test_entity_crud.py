"""memory_entities + entity_memory_links 表 CRUD 测试"""
import asyncio
import time
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
async def entity_db(tmp_path):
    """创建带 v13 schema 的测试数据库"""
    from db.database import DatabaseManager
    db_path = tmp_path / "test_entity.db"
    db = DatabaseManager(db_path)
    await db.init()
    yield db
    await db.close()


class TestMemoryEntityCRUD:
    """memory_entities 表 CRUD"""

    async def test_insert_entity(self, entity_db):
        """插入实体，返回 id"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        assert entity_id > 0

    async def test_insert_duplicate_returns_none(self, entity_db):
        """重复插入相同 (name, entity_type) 返回 None"""
        await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        result = await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        assert result is None

    async def test_find_by_name(self, entity_db):
        """按名称+类型查找实体"""
        await entity_db.memory.insert_memory_entity(
            name="张三", entity_type="PROPER", kind="人物"
        )
        entity = await entity_db.memory.find_memory_entity_by_name("张三", "PROPER")
        assert entity is not None
        assert entity["name"] == "张三"
        assert entity["entity_type"] == "PROPER"
        assert entity["kind"] == "人物"

    async def test_find_by_name_not_found(self, entity_db):
        """查找不存在的实体返回 None"""
        entity = await entity_db.memory.find_memory_entity_by_name("不存在", "TOPIC")
        assert entity is None

    async def test_increment_memory_count(self, entity_db):
        """递增实体链接的记忆数"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="React", entity_type="IDENTIFIER", kind="技术"
        )
        await entity_db.memory.increment_entity_memory_count(entity_id)
        await entity_db.memory.increment_entity_memory_count(entity_id)
        entity = await entity_db.memory.find_memory_entity_by_id(entity_id)
        assert entity["memory_count"] == 2

    async def test_update_last_seen(self, entity_db):
        """更新实体最后出现时间"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Vue", entity_type="IDENTIFIER", kind="技术"
        )
        new_ts = time.time() + 1000
        await entity_db.memory.update_entity_last_seen(entity_id, new_ts)
        entity = await entity_db.memory.find_memory_entity_by_id(entity_id)
        assert entity["last_seen"] == new_ts

    async def test_search_entity_by_fts(self, entity_db):
        """通过 FTS 模糊搜索实体"""
        await entity_db.memory.insert_memory_entity(
            name="人工智能", entity_type="TOPIC", kind="概念"
        )
        await entity_db.memory.insert_memory_entity(
            name="深度学习", entity_type="TOPIC", kind="技术"
        )
        results = await entity_db.memory.search_entities_by_fts("人工", limit=5)
        assert len(results) >= 1
        assert any(r["name"] == "人工智能" for r in results)


class TestEntityMemoryLinksCRUD:
    """entity_memory_links 表 CRUD"""

    async def test_insert_link(self, entity_db):
        """插入实体↔记忆反向链接"""
        # 先创建实体和记忆
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        mem_id = await entity_db.memory.insert_episodic_memory(summary="学习Python编程")

        link_id = await entity_db.memory.insert_entity_memory_link(
            entity_id=entity_id, memory_id=mem_id, confidence=0.95
        )
        assert link_id is not None
        assert link_id > 0

    async def test_insert_duplicate_link_returns_none(self, entity_db):
        """重复插入相同 (entity_id, memory_id) 返回 None"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Java", entity_type="IDENTIFIER", kind="技术"
        )
        mem_id = await entity_db.memory.insert_episodic_memory(summary="学习Java")

        await entity_db.memory.insert_entity_memory_link(entity_id, mem_id)
        result = await entity_db.memory.insert_entity_memory_link(entity_id, mem_id)
        assert result is None

    async def test_get_links_by_entity(self, entity_db):
        """按实体 ID 查询反向链接的记忆"""
        entity_id = await entity_db.memory.insert_memory_entity(
            name="Rust", entity_type="IDENTIFIER", kind="技术"
        )
        mem1 = await entity_db.memory.insert_episodic_memory(summary="Rust入门")
        mem2 = await entity_db.memory.insert_episodic_memory(summary="Rust进阶")

        await entity_db.memory.insert_entity_memory_link(entity_id, mem1)
        await entity_db.memory.insert_entity_memory_link(entity_id, mem2)

        links = await entity_db.memory.get_entity_memory_links(entity_id)
        assert len(links) == 2
        memory_ids = [l["memory_id"] for l in links]
        assert mem1 in memory_ids
        assert mem2 in memory_ids

    async def test_get_memories_by_entity_names_scoped(self, entity_db):
        """按实体名列表 + scope 反查记忆"""
        from memory.scope import Scope
        scope = Scope(user_id="alice", agent_id="xiaoli")

        entity_id = await entity_db.memory.insert_memory_entity(
            name="TypeScript", entity_type="IDENTIFIER", kind="技术"
        )
        mem_id = await entity_db.memory.insert_episodic_memory(
            summary="TypeScript类型系统", scope=scope
        )
        await entity_db.memory.insert_entity_memory_link(entity_id, mem_id)

        results = await entity_db.memory.get_memories_by_entity_names_scoped(
            ["TypeScript"], scope=scope, limit=10
        )
        assert len(results) == 1
        assert results[0]["summary"] == "TypeScript类型系统"

    async def test_get_memories_by_entity_names_scoped_isolated(self, entity_db):
        """不同 scope 的记忆互不串"""
        from memory.scope import Scope
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        entity_id = await entity_db.memory.insert_memory_entity(
            name="Go", entity_type="IDENTIFIER", kind="技术"
        )
        mem_alice = await entity_db.memory.insert_episodic_memory(
            summary="alice的Go笔记", scope=scope_alice
        )
        mem_bob = await entity_db.memory.insert_episodic_memory(
            summary="bob的Go笔记", scope=scope_bob
        )
        await entity_db.memory.insert_entity_memory_link(entity_id, mem_alice)
        await entity_db.memory.insert_entity_memory_link(entity_id, mem_bob)

        # alice scope 只查到 alice 的记忆
        results_alice = await entity_db.memory.get_memories_by_entity_names_scoped(
            ["Go"], scope=scope_alice, limit=10
        )
        assert len(results_alice) == 1
        assert results_alice[0]["summary"] == "alice的Go笔记"

        # bob scope 只查到 bob 的记忆
        results_bob = await entity_db.memory.get_memories_by_entity_names_scoped(
            ["Go"], scope=scope_bob, limit=10
        )
        assert len(results_bob) == 1
        assert results_bob[0]["summary"] == "bob的Go笔记"
