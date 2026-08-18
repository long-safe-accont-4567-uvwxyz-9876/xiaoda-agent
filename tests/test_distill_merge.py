"""蒸馏流程测试：merge_knowledge + _distill_to_knowledge + _update_knowledge"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

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
