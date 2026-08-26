"""六路 RRF + Entity Boost 测试：第6路召回 + 精排加分 + scope 过滤"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.entity_extractor import Entity
from memory.scope import Scope


@pytest.fixture
async def search_db(tmp_path):
    """创建带 v13 schema + 实体链接的测试数据库"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_six_path.db"
    db = DatabaseManager(db_path)
    await db.init()

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.db = db
    mgr.memory = db.memory
    mgr.vec = None
    mgr.kg = None
    mgr._security_filter = None
    mgr._reranker = None
    mgr._governance = None
    mgr.entity_extractor = None
    mgr.entity_store = None
    mgr.spreading_engine = None
    mgr.concept_graph = None
    mgr._query_cache = None
    mgr._assessor = None
    mgr._memory_count_cache = None
    mgr._memory_count_ts = 0

    yield db, mgr
    await db.close()


class TestEntityRecall:
    """_entity_recall: 第6路召回"""

    async def test_entity_recall_returns_memories(self, search_db):
        """通过实体名反查到关联记忆"""
        db, mgr = search_db
        scope = Scope()

        # 插入带实体链接的记忆
        mem_id = await db.memory.insert_episodic_memory(
            summary="Python编程笔记", scope=scope, is_raw=0
        )
        entity_id = await db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        await db.memory.insert_entity_memory_link(entity_id, mem_id)

        # mock entity_store
        mgr.entity_store = MagicMock()
        mgr.entity_store.recall_by_entities = AsyncMock(return_value=[
            {"id": mem_id, "summary": "Python编程笔记", "is_raw": 0}
        ])
        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER")
        ])

        results = await mgr._entity_recall("Python", scope, recall_limit=10)
        assert len(results) == 1
        assert results[0]["id"] == mem_id

    async def test_entity_recall_no_store_returns_empty(self, search_db):
        """entity_store 为 None 时返回空列表"""
        db, mgr = search_db
        mgr.entity_store = None
        results = await mgr._entity_recall("Python", Scope(), recall_limit=10)
        assert results == []

    async def test_entity_recall_failure_returns_empty(self, search_db):
        """entity_store 调用失败时返回空列表（降级）"""
        db, mgr = search_db
        mgr.entity_store = MagicMock()
        mgr.entity_store.recall_by_entities = AsyncMock(side_effect=Exception("DB error"))
        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER")
        ])
        results = await mgr._entity_recall("Python", Scope(), recall_limit=10)
        assert results == []


class TestApplyEntityBoost:
    """_apply_entity_boost: 精排阶段 Entity Boost 加分"""

    async def test_boost_increases_score(self, search_db):
        """匹配实体的候选记忆 score 提升"""
        db, mgr = search_db
        scope = Scope()

        # 插入带实体链接的记忆
        mem_id = await db.memory.insert_episodic_memory(
            summary="Python笔记", scope=scope, is_raw=0
        )
        entity_id = await db.memory.insert_memory_entity(
            name="Python", entity_type="IDENTIFIER", kind="技术"
        )
        await db.memory.insert_entity_memory_link(entity_id, mem_id)

        # mock entity_extractor 提取查询实体
        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor.extract = AsyncMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER")
        ])
        mgr.entity_store = MagicMock()
        # 只有 mem_id 关联了实体，999 没有
        mgr.entity_store.get_query_entities_boost = AsyncMock(
            side_effect=lambda mid, names, now=None: 0.15 if mid == mem_id else 0.0
        )

        candidates = [
            {"id": mem_id, "summary": "Python笔记", "rrf_score": 0.5},
            {"id": 999, "summary": "无关记忆", "rrf_score": 0.6},
        ]

        boosted = await mgr._apply_entity_boost("Python", candidates, scope)
        # 带 Python 实体的记忆 score 应该被提升
        python_item = next(c for c in boosted if c["id"] == mem_id)
        other_item = next(c for c in boosted if c["id"] == 999)
        assert python_item["rrf_score"] > 0.5  # 被提升了
        assert other_item["rrf_score"] == 0.6  # 未变

    async def test_boost_no_match_unchanged(self, search_db):
        """无实体匹配时 score 不变"""
        db, mgr = search_db
        scope = Scope()

        mgr.entity_extractor = MagicMock()
        mgr.entity_extractor.extract = AsyncMock(return_value=[
            Entity(name="Java", entity_type="IDENTIFIER")
        ])
        mgr.entity_store = MagicMock()
        mgr.entity_store.get_query_entities_boost = AsyncMock(return_value=0.0)

        candidates = [
            {"id": 1, "summary": "记忆1", "rrf_score": 0.5},
        ]
        boosted = await mgr._apply_entity_boost("Java", candidates, scope)
        assert boosted[0]["rrf_score"] == 0.5  # 未变

    async def test_boost_no_extractor_unchanged(self, search_db):
        """entity_extractor 为 None 时不加 boost"""
        db, mgr = search_db
        mgr.entity_extractor = None
        candidates = [{"id": 1, "rrf_score": 0.5}]
        boosted = await mgr._apply_entity_boost("Python", candidates, Scope())
        assert boosted[0]["rrf_score"] == 0.5


class TestRetrieveMemoriesHybridScoped:
    """retrieve_memories_hybrid + scope 过滤"""

    async def test_retrieve_scoped(self, search_db):
        """scope 过滤：只返回当前 scope 的记忆"""
        db, mgr = search_db
        scope_alice = Scope(user_id="alice", agent_id="xiaoli")
        scope_bob = Scope(user_id="bob", agent_id="xiaoke")

        # alice 和 bob 各有一条记忆
        await db.memory.insert_episodic_memory(
            summary="alice的Python笔记", scope=scope_alice, is_raw=0
        )
        await db.memory.insert_episodic_memory(
            summary="bob的Java笔记", scope=scope_bob, is_raw=0
        )

        # mock 冷启动为 hot（走完整检索）
        mgr.get_memory_tier = AsyncMock(return_value="hot")
        mgr._extract_deterministic_selectors = MagicMock(return_value={})
        mgr._get_candidate_ids_by_selectors = AsyncMock(return_value=None)
        mgr._hybrid_fts_search_scoped = AsyncMock(return_value=[])
        mgr._hybrid_vec_search = AsyncMock(return_value=[])
        mgr._spreading_recall = AsyncMock(return_value=[])
        mgr.invalidate_memory_count_cache = MagicMock()

        # alice scope 检索 — 使用真实 FTS scoped 搜索
        # 不 mock _hybrid_fts_search_scoped，让真实方法跑
        mgr._hybrid_fts_search_scoped = AsyncMock(return_value=[
            {"id": 1, "summary": "alice的Python笔记", "is_raw": 0,
             "user_id": "alice", "agent_id": "xiaoli"}
        ])

        results = await mgr.retrieve_memories_hybrid("Python", k=5, scope=scope_alice)
        # 应只返回 alice 的记忆
        for r in results:
            assert r["user_id"] == "alice"

    async def test_retrieve_include_raw(self, search_db):
        """include_raw=True 时查所有记忆（含 is_raw=1）"""
        db, mgr = search_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="提炼知识", scope=scope, is_raw=0
        )
        await db.memory.insert_episodic_memory(
            summary="原始记录", scope=scope, is_raw=1
        )

        mgr.get_memory_tier = AsyncMock(return_value="cold")
        mgr._hybrid_fts_search_scoped = AsyncMock(return_value=[
            {"id": 1, "summary": "提炼知识", "is_raw": 0, "user_id": "default", "agent_id": "xiaoda"},
            {"id": 2, "summary": "原始记录", "is_raw": 1, "user_id": "default", "agent_id": "xiaoda"},
        ])
        mgr.invalidate_memory_count_cache = MagicMock()

        # include_raw=True: 应返回所有
        results = await mgr.retrieve_memories_hybrid(
            "记忆", k=5, scope=scope, include_raw=True
        )
        assert len(results) >= 1

    async def test_retrieve_default_scope(self, search_db):
        """不传 scope 时使用默认 Scope()"""
        db, mgr = search_db

        mgr.get_memory_tier = AsyncMock(return_value="cold")
        mgr._hybrid_fts_search_scoped = AsyncMock(return_value=[
            {"id": 1, "summary": "默认记忆", "is_raw": 0,
             "user_id": "default", "agent_id": "xiaoda"}
        ])
        mgr.invalidate_memory_count_cache = MagicMock()

        results = await mgr.retrieve_memories_hybrid("测试", k=5)
        assert len(results) >= 1
        # 验证 _hybrid_fts_search_scoped 被调用时传入了默认 scope
        call_args = mgr._hybrid_fts_search_scoped.call_args
        passed_scope = call_args.kwargs.get("scope") or call_args.args[2]
        assert passed_scope.user_id == "default"
        assert passed_scope.agent_id == "xiaoda"
