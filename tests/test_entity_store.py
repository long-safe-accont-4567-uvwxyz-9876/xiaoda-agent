"""EntityStore 测试：实体存储 + 反向链接 + recall_by_entities + Entity Boost"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.entity_extractor import Entity
from memory.entity_store import ENTITY_BOOST_WEIGHT, EntityStore, compute_entity_boost
from memory.scope import Scope


@pytest.fixture
async def store_db(tmp_path):
    """创建带 v13 schema 的测试数据库 + EntityStore"""
    from db.database import DatabaseManager
    db_path = tmp_path / "test_entity_store.db"
    db = DatabaseManager(db_path)
    await db.init()
    store = EntityStore(db.memory)
    yield db, store
    await db.close()


class TestLinkEntities:
    """link_entities: 实体链接到记忆"""

    async def test_link_new_entity(self, store_db):
        """新实体：创建实体 + 建立反向链接"""
        db, store = store_db
        mem_id = await db.memory.insert_episodic_memory(summary="学习Python编程")
        entities = [Entity(name="Python", entity_type="IDENTIFIER", kind="技术")]

        linked = await store.link_entities(mem_id, entities, scope=Scope())
        assert linked == 1

        # 验证实体已创建
        entity = await db.memory.find_memory_entity_by_name("Python", "IDENTIFIER")
        assert entity is not None
        assert entity["memory_count"] == 1

        # 验证反向链接已建立
        links = await db.memory.get_entity_memory_links(entity["id"])
        assert len(links) == 1
        assert links[0]["memory_id"] == mem_id

    async def test_link_existing_entity(self, store_db):
        """已有实体：只建立反向链接，不重复创建"""
        db, store = store_db
        # 先创建实体
        _entity_id = await db.memory.insert_memory_entity(
            name="React", entity_type="IDENTIFIER", kind="技术"
        )
        # 链接到第一条记忆
        mem1 = await db.memory.insert_episodic_memory(summary="React入门")
        await store.link_entities(mem1, [Entity(name="React", entity_type="IDENTIFIER")], scope=Scope())

        # 链接到第二条记忆
        mem2 = await db.memory.insert_episodic_memory(summary="React进阶")
        await store.link_entities(mem2, [Entity(name="React", entity_type="IDENTIFIER")], scope=Scope())

        # 验证实体只创建了一个
        entity = await db.memory.find_memory_entity_by_name("React", "IDENTIFIER")
        assert entity is not None
        assert entity["memory_count"] == 2

        # 验证有两条反向链接
        links = await db.memory.get_entity_memory_links(entity["id"])
        assert len(links) == 2

    async def test_link_multiple_entities(self, store_db):
        """一次链接多个实体"""
        db, store = store_db
        mem_id = await db.memory.insert_episodic_memory(summary="张三学习Python和React")
        entities = [
            Entity(name="张三", entity_type="PROPER", kind="人物"),
            Entity(name="Python", entity_type="IDENTIFIER", kind="技术"),
            Entity(name="React", entity_type="IDENTIFIER", kind="技术"),
        ]
        linked = await store.link_entities(mem_id, entities, scope=Scope())
        assert linked == 3

    async def test_link_empty_entities(self, store_db):
        """空实体列表返回 0"""
        db, store = store_db
        mem_id = await db.memory.insert_episodic_memory(summary="无实体记忆")
        linked = await store.link_entities(mem_id, [], scope=Scope())
        assert linked == 0

    async def test_link_updates_last_seen(self, store_db):
        """链接时更新实体 last_seen（时间感知）"""
        db, store = store_db
        # 先创建实体
        old_ts = time.time() - 86400  # 1天前
        entity_id = await db.memory.insert_memory_entity(
            name="OldEntity", entity_type="TOPIC", kind="概念"
        )
        # 手动设置旧的 last_seen
        await db.memory.update_entity_last_seen(entity_id, old_ts)

        # 链接到新记忆（应更新 last_seen）
        mem_id = await db.memory.insert_episodic_memory(summary="新记忆")
        await store.link_entities(
            mem_id, [Entity(name="OldEntity", entity_type="TOPIC")], scope=Scope()
        )

        entity = await db.memory.find_memory_entity_by_id(entity_id)
        assert entity["last_seen"] > old_ts


class TestRecallByEntities:
    """recall_by_entities: 通过实体名反向查询记忆"""

    async def test_recall_single_entity(self, store_db):
        """单实体反查记忆"""
        db, store = store_db
        scope = Scope(user_id="alice", agent_id="xiaoli")
        mem_id = await db.memory.insert_episodic_memory(
            summary="alice的Python笔记", scope=scope
        )
        await store.link_entities(
            mem_id, [Entity(name="Python", entity_type="IDENTIFIER")], scope=scope
        )

        results = await store.recall_by_entities(["Python"], scope=scope, limit=10)
        assert len(results) >= 1
        assert any(r["id"] == mem_id for r in results)

    async def test_recall_multiple_entities(self, store_db):
        """多实体反查记忆（UNION）"""
        db, store = store_db
        scope = Scope()
        mem1 = await db.memory.insert_episodic_memory(summary="Python笔记")
        mem2 = await db.memory.insert_episodic_memory(summary="React笔记")
        await store.link_entities(
            mem1, [Entity(name="Python", entity_type="IDENTIFIER")], scope=scope
        )
        await store.link_entities(
            mem2, [Entity(name="React", entity_type="IDENTIFIER")], scope=scope
        )

        results = await store.recall_by_entities(["Python", "React"], scope=scope, limit=10)
        mem_ids = [r["id"] for r in results]
        assert mem1 in mem_ids
        assert mem2 in mem_ids

    async def test_recall_scope_isolated(self, store_db):
        """不同 scope 的记忆互不串"""
        db, store = store_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        mem_alice = await db.memory.insert_episodic_memory(
            summary="alice的Go笔记", scope=scope_alice
        )
        mem_bob = await db.memory.insert_episodic_memory(
            summary="bob的Go笔记", scope=scope_bob
        )

        # 共享同一实体名，但不同 scope
        await store.link_entities(
            mem_alice, [Entity(name="Go", entity_type="IDENTIFIER")], scope=scope_alice
        )
        await store.link_entities(
            mem_bob, [Entity(name="Go", entity_type="IDENTIFIER")], scope=scope_bob
        )

        # alice scope 只查到 alice 的记忆
        results_alice = await store.recall_by_entities(["Go"], scope=scope_alice, limit=10)
        mem_ids = [r["id"] for r in results_alice]
        assert mem_alice in mem_ids
        assert mem_bob not in mem_ids

    async def test_recall_empty_names(self, store_db):
        """空实体名列表返回空"""
        db, store = store_db
        results = await store.recall_by_entities([], scope=Scope(), limit=10)
        assert results == []

    async def test_recall_no_match(self, store_db):
        """无匹配实体返回空"""
        db, store = store_db
        results = await store.recall_by_entities(
            ["不存在的实体"], scope=Scope(), limit=10
        )
        assert results == []


class TestEntityBoost:
    """compute_entity_boost: Entity Boost 计算"""

    def test_boost_match(self):
        """实体匹配查询实体时 boost > 0"""
        entity = {"name": "Python", "memory_count": 5, "last_seen": time.time()}
        query_entities = {"Python", "React"}
        boost = compute_entity_boost(entity, query_entities, now=time.time())
        assert boost > 0
        assert boost <= ENTITY_BOOST_WEIGHT

    def test_boost_no_match(self):
        """实体不匹配查询实体时 boost = 0"""
        entity = {"name": "Java", "memory_count": 5, "last_seen": time.time()}
        query_entities = {"Python", "React"}
        boost = compute_entity_boost(entity, query_entities, now=time.time())
        assert boost == 0.0

    def test_boost_recency_decay(self):
        """时间衰减：旧的实体 boost 更低"""
        now = time.time()
        entity_recent = {"name": "Python", "memory_count": 5, "last_seen": now}
        entity_old = {"name": "Python", "memory_count": 5, "last_seen": now - 86400 * 30}

        query_entities = {"Python"}
        boost_recent = compute_entity_boost(entity_recent, query_entities, now=now)
        boost_old = compute_entity_boost(entity_old, query_entities, now=now)
        assert boost_recent > boost_old

    def test_boost_memory_count_diminishing(self):
        """记忆数边际递减：count=100 vs count=5 的 boost 差距不大"""
        now = time.time()
        entity_few = {"name": "Python", "memory_count": 5, "last_seen": now}
        entity_many = {"name": "Python", "memory_count": 100, "last_seen": now}
        query_entities = {"Python"}

        boost_few = compute_entity_boost(entity_few, query_entities, now=now)
        boost_many = compute_entity_boost(entity_many, query_entities, now=now)
        # 两者都 > 0，且 many >= few（边际递减但不为负）
        assert boost_few > 0
        assert boost_many > 0
        assert boost_many >= boost_few * 0.9  # 差距不大
