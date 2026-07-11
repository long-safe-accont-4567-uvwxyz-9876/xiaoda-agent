"""时间感知增强测试：_try_temporal_search 加入 scope 过滤 + EntityStore last_seen 更新"""
import asyncio
import time
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.scope import Scope
from memory.entity_extractor import Entity
from memory.entity_store import EntityStore, compute_entity_boost


@pytest.fixture
async def temporal_db(tmp_path):
    """创建带 v13 schema 的测试数据库"""
    from db.database import DatabaseManager
    db_path = tmp_path / "test_temporal.db"
    db = DatabaseManager(db_path)
    await db.init()
    yield db
    await db.close()


class TestTemporalSearchScoped:
    """_try_temporal_search 加入 scope 过滤"""

    async def test_temporal_search_scope_filtered(self, temporal_db):
        """时间检索只返回当前 scope 的记忆"""
        db = temporal_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")
        now = time.time()
        yesterday = now - 86400

        # alice 和 bob 各有一条昨天的记忆
        await db.memory.insert_episodic_memory(
            summary="alice昨天的记忆", scope=scope_alice, is_raw=0
        )
        await db._conn.execute(
            "UPDATE episodic_memories SET timestamp=? WHERE user_id='alice'",
            (yesterday,),
        )
        await db._conn.commit()

        await db.memory.insert_episodic_memory(
            summary="bob昨天的记忆", scope=scope_bob, is_raw=0
        )
        await db._conn.execute(
            "UPDATE episodic_memories SET timestamp=? WHERE user_id='bob'",
            (yesterday,),
        )
        await db._conn.commit()

        # alice scope 查询昨天的记忆
        from memory.memory_manager import MemoryManager
        mgr = MemoryManager.__new__(MemoryManager)
        mgr.memory = db.memory
        mgr._reranker = None

        results = await mgr._try_temporal_search("昨天发生了什么", k=10, scope=scope_alice)
        # 应只返回 alice 的记忆
        assert len(results) >= 1
        for r in results:
            assert r["user_id"] == "alice"

    async def test_temporal_search_no_time_word(self, temporal_db):
        """无时间词返回空"""
        from memory.memory_manager import MemoryManager
        db = temporal_db
        mgr = MemoryManager.__new__(MemoryManager)
        mgr.memory = db.memory
        mgr._reranker = None
        results = await mgr._try_temporal_search("Python编程", k=10, scope=Scope())
        assert results == [] or results is None

    async def test_temporal_search_is_raw_filter(self, temporal_db):
        """时间检索默认只查 is_raw=0"""
        db = temporal_db
        scope = Scope()
        yesterday = time.time() - 86400

        # 插入一条 is_raw=0 和一条 is_raw=1
        mem_raw = await db.memory.insert_episodic_memory(
            summary="原始记录昨天", scope=scope, is_raw=1
        )
        mem_refined = await db.memory.insert_episodic_memory(
            summary="提炼知识昨天", scope=scope, is_raw=0
        )
        # 手动更新时间戳
        await db._conn.execute(
            "UPDATE episodic_memories SET timestamp=? WHERE id IN (?, ?)",
            (yesterday, mem_raw, mem_refined),
        )
        await db._conn.commit()

        from memory.memory_manager import MemoryManager
        mgr = MemoryManager.__new__(MemoryManager)
        mgr.memory = db.memory
        mgr._reranker = None

        # 默认 include_raw=False
        results = await mgr._try_temporal_search("昨天", k=10, scope=scope)
        assert len(results) >= 1
        for r in results:
            assert r["is_raw"] == 0


class TestEntityLastSeenUpdate:
    """EntityStore 链接时更新 last_seen（时间感知）"""

    async def test_link_updates_last_seen(self, temporal_db):
        """链接实体时 last_seen 被更新为当前时间"""
        db = temporal_db
        store = EntityStore(db.memory)

        # 先创建实体（设置旧的 last_seen）
        entity_id = await db.memory.insert_memory_entity(
            name="OldTopic", entity_type="TOPIC", kind="概念"
        )
        old_ts = time.time() - 86400 * 7  # 7天前
        await db.memory.update_entity_last_seen(entity_id, old_ts)

        # 链接到新记忆
        mem_id = await db.memory.insert_episodic_memory(summary="新记忆")
        await store.link_entities(
            mem_id, [Entity(name="OldTopic", entity_type="TOPIC")], scope=Scope()
        )

        entity = await db.memory.find_memory_entity_by_id(entity_id)
        assert entity["last_seen"] > old_ts

    async def test_boost_recency_decay_integration(self, temporal_db):
        """Entity Boost 时间衰减集成测试"""
        db = temporal_db
        now = time.time()

        # 最近实体（今天）
        entity_recent = {"name": "Python", "memory_count": 5, "last_seen": now}
        # 旧实体（30天前）
        entity_old = {"name": "Python", "memory_count": 5, "last_seen": now - 86400 * 30}

        query_entities = {"Python"}
        boost_recent = compute_entity_boost(entity_recent, query_entities, now=now)
        boost_old = compute_entity_boost(entity_old, query_entities, now=now)

        # 最近的 boost 应该明显高于旧的
        assert boost_recent > boost_old
        assert boost_recent > 0
        assert boost_old > 0  # 旧的不为0，只是衰减
