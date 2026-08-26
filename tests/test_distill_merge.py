"""蒸馏流程测试：merge_knowledge + _distill_to_knowledge + _update_knowledge"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.context_governance import ContextGovernance, compute_content_hash
from memory.fsrs_model import S_PERMANENT
from memory.memory_distiller import MemoryDistiller
from memory.scope import Scope


@pytest.fixture
async def distill_db(tmp_path):
    """创建带 v13 schema 的测试数据库 + MemoryManager"""
    from db.database import DatabaseManager
    from memory.memory_manager import MemoryManager
    db_path = tmp_path / "test_distill.db"
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
    mgr.distiller = MemoryDistiller(router=None)

    yield db, mgr
    await db.close()


class TestMergeKnowledge:
    """MemoryDistiller.merge_knowledge: LLM 合并相似知识"""

    def _make_distiller(self):
        """创建带 mock 的 distiller"""
        distiller = MemoryDistiller(router=None)
        distiller._free_api_key = "fake-key"
        return distiller

    async def test_merge_success(self):
        """LLM 合并两段知识"""
        distiller = self._make_distiller()
        distiller._call_free_model = AsyncMock(return_value="合并后的知识：用户喜欢Python和React")
        result = await distiller.merge_knowledge(
            existing="用户喜欢Python",
            new_content="用户也喜欢React",
        )
        assert result == "合并后的知识：用户喜欢Python和React"
        distiller._call_free_model.assert_awaited_once()

    async def test_merge_failure_returns_concat(self):
        """LLM 合并失败时返回 existing + new_content（保留旧知识）"""
        distiller = self._make_distiller()
        distiller._call_free_model = AsyncMock(return_value=None)
        result = await distiller.merge_knowledge(
            existing="旧知识",
            new_content="新知识",
        )
        assert "旧知识" in result
        assert "新知识" in result

    async def test_merge_empty_existing(self):
        """existing 为空时直接返回 new_content"""
        distiller = self._make_distiller()
        distiller._call_free_model = AsyncMock(return_value="should_not_be_used")
        result = await distiller.merge_knowledge(existing="", new_content="新知识")
        assert result == "新知识"
        distiller._call_free_model.assert_not_awaited()


class TestDistillToKnowledge:
    """_distill_to_knowledge: 原始记忆 → 提炼知识"""

    async def test_distill_creates_new_knowledge(self, distill_db):
        """无相似知识时新建 is_raw=0 的提炼知识"""
        db, mgr = distill_db
        scope = Scope()

        # 插入原始记忆
        raw_id = await db.memory.insert_episodic_memory(
            summary="原始记录：用户喜欢Python", scope=scope, is_raw=1
        )

        # mock distiller.distill 返回蒸馏结果
        mgr.distiller.distill = AsyncMock(return_value="用户喜欢Python编程")
        mgr.distiller.merge_knowledge = AsyncMock(return_value="合并知识")
        # mock _find_similar_knowledge 返回 None（无相似）
        mgr._find_similar_knowledge = AsyncMock(return_value=None)

        await mgr._distill_to_knowledge(raw_id, "原始记录：用户喜欢Python", scope, 0.8, "开心")

        # 验证创建了 is_raw=0 的提炼知识
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE is_raw=0 AND user_id=? AND agent_id=?",
            (scope.user_id, scope.agent_id),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["summary"] == "用户喜欢Python编程"

    async def test_distill_updates_existing_knowledge(self, distill_db):
        """有相似知识时 UPDATE（合并）"""
        db, mgr = distill_db
        scope = Scope()

        # 先插入一条提炼知识
        existing_id = await db.memory.insert_episodic_memory(
            summary="用户喜欢Python", scope=scope, is_raw=0
        )

        # 插入原始记忆
        raw_id = await db.memory.insert_episodic_memory(
            summary="用户也喜欢React", scope=scope, is_raw=1
        )

        # mock 返回相似知识
        existing_mem = await db.memory.get_memory_by_id(existing_id)
        mgr._find_similar_knowledge = AsyncMock(return_value=existing_mem)
        mgr.distiller.merge_knowledge = AsyncMock(return_value="用户喜欢Python和React")
        mgr.distiller.distill = AsyncMock(return_value="用户也喜欢React")

        await mgr._distill_to_knowledge(raw_id, "用户也喜欢React", scope, 0.5, "")

        # 验证提炼知识被 UPDATE（合并）
        cursor = await db._conn.execute(
            "SELECT * FROM episodic_memories WHERE id=?", (existing_id,)
        )
        row = await cursor.fetchone()
        assert row["summary"] == "用户喜欢Python和React"

    async def test_distill_no_result_skips(self, distill_db):
        """蒸馏返回空时跳过（不创建提炼知识）"""
        db, mgr = distill_db
        scope = Scope()

        raw_id = await db.memory.insert_episodic_memory(
            summary="原始记录", scope=scope, is_raw=1
        )

        mgr.distiller.distill = AsyncMock(return_value="")  # 蒸馏失败

        await mgr._distill_to_knowledge(raw_id, "原始记录", scope, 0.5, "")

        # 验证没有创建 is_raw=0 的记录
        cursor = await db._conn.execute(
            "SELECT COUNT(*) as cnt FROM episodic_memories WHERE is_raw=0"
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0


class TestFindSimilarKnowledge:
    """_find_similar_knowledge: 查找相似提炼知识"""

    async def test_find_similar_exists(self, distill_db):
        """找到相似的 is_raw=0 知识"""
        db, mgr = distill_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="用户喜欢Python编程语言", scope=scope, is_raw=0
        )

        similar = await mgr._find_similar_knowledge("用户喜欢Python", scope=scope)
        assert similar is not None
        assert "Python" in similar["summary"]

    async def test_find_similar_not_found(self, distill_db):
        """无相似知识返回 None"""
        db, mgr = distill_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="完全不同的内容关于天气", scope=scope, is_raw=0
        )

        similar = await mgr._find_similar_knowledge("Python编程", scope=scope)
        assert similar is None

    async def test_find_similar_ignores_raw(self, distill_db):
        """只查 is_raw=0，忽略 is_raw=1"""
        db, mgr = distill_db
        scope = Scope()

        await db.memory.insert_episodic_memory(
            summary="原始记录Python", scope=scope, is_raw=1
        )

        similar = await mgr._find_similar_knowledge("原始记录Python", scope=scope)
        assert similar is None  # is_raw=1 不参与

    async def test_find_similar_rejects_loose_match(self, distill_db):
        """Jaccard 阈值过滤：共同 token 多但核心内容不同时不匹配"""
        db, mgr = distill_db
        scope = Scope()

        # 已有知识 "用户喜欢Java"
        await db.memory.insert_episodic_memory(
            summary="用户喜欢Java", scope=scope, is_raw=0
        )

        # 查询 "用户喜欢Python" — FTS 会因 "用户"、"喜欢" 命中，
        # 但 Jaccard < 0.4 应被过滤
        similar = await mgr._find_similar_knowledge("用户喜欢Python", scope=scope)
        assert similar is None


class TestUpdateKnowledge:
    """_update_knowledge: 独立单元测试"""

    async def test_update_merges_content(self, distill_db):
        """_update_knowledge 调用 LLM 合并并更新记录"""
        db, mgr = distill_db
        scope = Scope()

        # 插入已有提炼知识
        existing_id = await db.memory.insert_episodic_memory(
            summary="用户喜欢Python", scope=scope, is_raw=0
        )

        # mock distiller.merge_knowledge 返回合并结果
        mgr.distiller.merge_knowledge = AsyncMock(
            return_value="用户喜欢Python和React"
        )
        mgr.vec = None  # 无向量

        raw_id = 999
        await mgr._update_knowledge(existing_id, "用户也喜欢React", raw_id, scope)

        # 验证记录被更新为合并后的内容
        updated = await db.memory.get_memory_by_id(existing_id)
        assert updated["summary"] == "用户喜欢Python和React"
        mgr.distiller.merge_knowledge.assert_awaited_once()

    async def test_update_promotes_canonical_state_from_fact_raw(self, distill_db):
        db, mgr = distill_db
        scope = Scope()
        canonical_id = await db.memory.insert_episodic_memory(
            "已有事件知识", importance=0.5, scope=scope, is_raw=0,
            memory_type="event", phase="buffer", stability=3.0,
            reinforcement_count=0,
        )
        raw_id = await db.memory.insert_episodic_memory(
            "我的生日是3月15日", importance=0.9, scope=scope, is_raw=1,
            memory_type="fact", phase="permanent", stability=S_PERMANENT,
            reinforcement_count=1,
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="合并后的生日知识")

        await mgr._update_knowledge(canonical_id, "生日事实", raw_id, scope)

        canonical = await db.memory.get_memory_by_id(canonical_id)
        raw = await db.memory.get_memory_by_id(raw_id)
        assert canonical["memory_type"] == "fact"
        assert canonical["importance"] >= 0.9
        assert canonical["phase"] == "permanent"
        assert canonical["stability"] >= S_PERMANENT
        assert canonical["reinforcement_count"] >= 1
        assert raw["summary"] == "我的生日是3月15日"

    @pytest.mark.parametrize("memory_type", ["affect", "relation"])
    async def test_update_promotes_event_canonical_to_reinforced_type(
        self, distill_db, memory_type
    ):
        db, mgr = distill_db
        scope = Scope()
        canonical_id = await db.memory.insert_episodic_memory(
            "已有事件知识", importance=0.5, scope=scope, is_raw=0,
            memory_type="event", phase="buffer", stability=3.0,
            reinforcement_count=0,
        )
        raw_id = await db.memory.insert_episodic_memory(
            "新的关系或情绪", importance=0.7, scope=scope, is_raw=1,
            memory_type=memory_type, phase="reinforced", stability=4.0,
            reinforcement_count=1,
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="合并知识")

        await mgr._update_knowledge(canonical_id, "新内容", raw_id, scope)

        canonical = await db.memory.get_memory_by_id(canonical_id)
        assert canonical["memory_type"] == memory_type
        assert canonical["importance"] >= 0.7
        assert canonical["phase"] == "reinforced"
        assert canonical["stability"] >= 4.0
        assert canonical["reinforcement_count"] >= 1

    async def test_update_does_not_lower_existing_fact_with_relation_raw(
        self, distill_db
    ):
        db, mgr = distill_db
        scope = Scope()
        canonical_id = await db.memory.insert_episodic_memory(
            "已有永久知识", importance=0.95, scope=scope, is_raw=0,
            memory_type="fact", phase="permanent", stability=S_PERMANENT,
            reinforcement_count=2,
        )
        raw_id = await db.memory.insert_episodic_memory(
            "新的关系", importance=0.7, scope=scope, is_raw=1,
            memory_type="relation", phase="reinforced", stability=4.0,
            reinforcement_count=1,
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="合并知识")

        await mgr._update_knowledge(canonical_id, "新内容", raw_id, scope)

        canonical = await db.memory.get_memory_by_id(canonical_id)
        assert canonical["memory_type"] == "fact"
        assert canonical["importance"] >= 0.95
        assert canonical["phase"] == "permanent"
        assert canonical["stability"] >= S_PERMANENT
        assert canonical["reinforcement_count"] >= 2

    async def test_update_instruction_promotes_default_event_canonical(self, distill_db):
        db, mgr = distill_db
        scope = Scope()
        canonical_id = await db.memory.insert_episodic_memory(
            "默认事件", scope=scope, is_raw=0, memory_type="event"
        )
        raw_id = await db.memory.insert_episodic_memory(
            "以后请记住规则", scope=scope, is_raw=1,
            memory_type="instruction", importance=0.7,
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="规则知识")

        await mgr._update_knowledge(canonical_id, "规则", raw_id, scope)

        canonical = await db.memory.get_memory_by_id(canonical_id)
        assert canonical["memory_type"] == "instruction"
        assert canonical["importance"] >= 0.7

    async def test_update_event_raw_does_not_lower_instruction_canonical(self, distill_db):
        db, mgr = distill_db
        scope = Scope()
        canonical_id = await db.memory.insert_episodic_memory(
            "已有规则", importance=0.8, scope=scope, is_raw=0,
            memory_type="instruction", phase="buffer",
        )
        raw_id = await db.memory.insert_episodic_memory(
            "普通事件", importance=0.5, scope=scope, is_raw=1,
            memory_type="event", phase="buffer",
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="规则附带事件")

        await mgr._update_knowledge(canonical_id, "事件", raw_id, scope)

        canonical = await db.memory.get_memory_by_id(canonical_id)
        assert canonical["memory_type"] == "instruction"
        assert canonical["importance"] >= 0.8

    async def test_update_merge_commits_main_fts_and_governance_atomically(self, distill_db):
        db, mgr = distill_db
        scope = Scope()
        mgr._governance = ContextGovernance(db._conn)
        canonical_id = await db.memory.insert_episodic_memory(
            "旧canonical摘要", importance=0.5, scope=scope, is_raw=0,
            memory_type="event",
        )
        await mgr._governance.record_initial_version(canonical_id, "旧canonical摘要")
        raw_id = await db.memory.insert_episodic_memory(
            "我的生日是3月15日", importance=0.9, scope=scope, is_raw=1,
            memory_type="fact", phase="permanent", stability=S_PERMANENT,
            reinforcement_count=1,
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="新canonical生日摘要")
        mgr.vec = MagicMock()
        mgr.vec.upsert = AsyncMock()

        await mgr._update_knowledge(canonical_id, "生日事实", raw_id, scope)

        canonical = await db.memory.get_memory_by_id(canonical_id)
        assert canonical["summary"] == "新canonical生日摘要"
        assert canonical["importance"] >= 0.9
        assert canonical["memory_type"] == "fact"
        assert canonical["phase"] == "permanent"
        assert canonical["stability"] >= S_PERMANENT
        assert canonical["reinforcement_count"] >= 1
        assert canonical["content_hash"] == compute_content_hash("新canonical生日摘要")
        assert canonical["version"] == 2
        versions = await db.fetch_all(
            "SELECT version, content_hash, summary_snapshot FROM memory_versions "
            "WHERE memory_id=? ORDER BY version",
            (canonical_id,),
        )
        assert [row["version"] for row in versions] == [1, 2]
        assert versions[-1]["summary_snapshot"] == "新canonical生日摘要"
        fts_rows = await db.fetch_all(
            "SELECT id FROM episodic_memory_fts WHERE id=? AND summary_index MATCH ?",
            (canonical_id, "生日"),
        )
        assert fts_rows == [{"id": canonical_id}]
        mgr.vec.upsert.assert_awaited_once_with(canonical_id, "新canonical生日摘要")

    @pytest.mark.parametrize("failure", ["fts", "db_false"])
    async def test_update_merge_failure_rolls_back_and_skips_vector(
        self, distill_db, failure
    ):
        db, mgr = distill_db
        scope = Scope()
        mgr._governance = ContextGovernance(db._conn)
        canonical_id = await db.memory.insert_episodic_memory(
            "事务前摘要", importance=0.5, scope=scope, is_raw=0,
            memory_type="event",
        )
        await mgr._governance.record_initial_version(canonical_id, "事务前摘要")
        before = await db.memory.get_memory_by_id(canonical_id)
        raw_id = await db.memory.insert_episodic_memory(
            "生日事实", importance=0.9, scope=scope, is_raw=1,
            memory_type="fact", phase="permanent", stability=S_PERMANENT,
            reinforcement_count=1,
        )
        mgr.distiller.merge_knowledge = AsyncMock(return_value="事务后摘要")
        mgr.vec = MagicMock()
        mgr.vec.upsert = AsyncMock()
        if failure == "fts":
            db.memory._sync_fts = AsyncMock(side_effect=RuntimeError("fts failed"))
        else:
            db.memory.merge_memory_knowledge_state = AsyncMock(return_value=False)

        await mgr._update_knowledge(canonical_id, "事实", raw_id, scope)

        after = await db.memory.get_memory_by_id(canonical_id)
        assert after["summary"] == before["summary"]
        assert after["importance"] == before["importance"]
        assert after["memory_type"] == before["memory_type"]
        assert after["phase"] == before["phase"]
        assert after["content_hash"] == before["content_hash"]
        assert after["version"] == before["version"]
        versions = await db.fetch_all(
            "SELECT version FROM memory_versions WHERE memory_id=? ORDER BY version",
            (canonical_id,),
        )
        assert versions == [{"version": 1}]
        mgr.vec.upsert.assert_not_awaited()

    async def test_update_nonexistent_knowledge_noop(self, distill_db):
        """_update_knowledge 对不存在的 ID 安全跳过"""
        db, mgr = distill_db
        scope = Scope()

        mgr.distiller.merge_knowledge = AsyncMock(return_value="合并")
        mgr.vec = None

        # 不存在的 knowledge_id
        await mgr._update_knowledge(99999, "新内容", 1, scope)

        # merge_knowledge 不应被调用（因为 existing 为 None 提前返回）
        mgr.distiller.merge_knowledge.assert_not_awaited()
