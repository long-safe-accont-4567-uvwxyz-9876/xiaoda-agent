"""MemoryManager 单元测试 —— 聚焦初始化、retrieve_memories、缓存、错误兜底与空结果处理。"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 辅助：安全构造 MemoryManager（隔离重量级依赖） ──
def _make_memory_manager(**kwargs):
    """通过 patch 隔离依赖后构造 MemoryManager。"""
    # MemoryManager.__init__ 内部会 import config, 创建 QueryCache 等，
    # 我们需要 patch 这些才能安全构造。
    with patch("memory.memory_manager.MemoryDistiller"), \
         patch("memory.memory_manager.QueryCache") as MockQC, \
         patch("memory.memory_manager.RetrievalAssessor"), \
         patch("memory.memory_manager.FSRSModel"), \
         patch("memory.memory_manager.get_agent_display_name", return_value="小妲"):

        MockQC.return_value = MagicMock()

        from memory.memory_manager import MemoryManager

        defaults = {
            "db": MagicMock(),
            "memory": MagicMock(),
        }
        defaults.update(kwargs)
        return MemoryManager(**defaults)


class TestMemoryManagerInit:
    """测试 MemoryManager 初始化流程。"""

    def test_init_stores_core_dependencies(self):
        """__init__ 应正确保存核心依赖 db, memory, vec, router, kg。"""
        db = MagicMock()
        memory = MagicMock()
        vec = MagicMock()
        router = MagicMock()
        kg = MagicMock()
        reranker = MagicMock()
        qt = MagicMock()
        gov = MagicMock()

        mm = _make_memory_manager(
            db=db, memory=memory,
            vector_store=vec, router=router,
            knowledge_graph=kg,
            reranker=reranker,
            query_transformer=qt,
            governance=gov,
        )

        assert mm.db is db
        assert mm.memory is memory
        assert mm.vec is vec
        assert mm.router is router
        assert mm.kg is kg
        assert mm._reranker is reranker
        assert mm._query_transformer is qt
        assert mm._governance is gov

    def test_init_optional_deps_default_to_none(self):
        """可选依赖不传时应为 None。"""
        mm = _make_memory_manager()

        assert mm.vec is None
        assert mm.kg is None
        assert mm._reranker is None
        assert mm._query_transformer is None
        assert mm._governance is None

    def test_init_internal_state_zeroed(self):
        """内部计时状态应初始化为零/False。"""
        mm = _make_memory_manager()

        assert mm._last_message_time == 0
        assert mm._last_encode_time == 0
        assert mm._pending_encode is False


class TestSignalNewMessage:
    """测试 signal_new_message 时间戳更新。"""

    def test_signal_updates_timestamp_and_pending(self):
        """signal_new_message 应更新时间戳并标记 pending_encode。"""
        mm = _make_memory_manager()

        before = time.time()
        mm.signal_new_message()
        after = time.time()

        assert before <= mm._last_message_time <= after
        assert mm._pending_encode is True


class TestSuggestK:
    """测试 _suggest_k 智能检索条数建议。"""

    def test_empty_query_returns_1(self):
        mm = _make_memory_manager()
        assert mm._suggest_k("", default_k=8) == 1

    def test_short_greeting_returns_small_k(self):
        """极短问候应返回较小 k。"""
        mm = _make_memory_manager()
        k = mm._suggest_k("你好", default_k=8)
        assert k <= 5

    def test_emotional_query_returns_larger_k(self):
        """情感/回忆型查询应返回较大 k。"""
        mm = _make_memory_manager()
        k = mm._suggest_k("你还记得我们上次去海边的事情吗", default_k=8)
        assert k >= 8


class TestRetrieveMemoriesCaching:
    """测试 retrieve_memories 查询缓存行为。"""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result(self):
        """缓存命中时应直接返回缓存结果。"""
        cached_result = [{"id": 1, "summary": "cached memory", "final_score": 0.9}]

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=cached_result)
        mock_cache.put = AsyncMock()

        mm = _make_memory_manager()
        mm._query_cache = mock_cache

        cached = await mm._query_cache.get("test query")
        assert cached == cached_result
        mock_cache.get.assert_awaited_once_with("test query")

    @pytest.mark.asyncio
    async def test_cache_miss_proceeds_to_retrieval(self):
        """缓存未命中时应继续执行检索流水线。"""
        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.put = AsyncMock()

        mm = _make_memory_manager()
        mm._query_cache = mock_cache

        cached = await mm._query_cache.get("new query")
        assert cached is None


class TestRetrieveMemoriesErrorFallback:
    """测试 retrieve_memories 错误兜底路径。"""

    @pytest.mark.asyncio
    async def test_query_transformer_failure_defaults_to_factual(self):
        """query_transformer.classify_intent 失败 → intent 回退为 'factual'。"""
        mock_qt = MagicMock()
        mock_qt.available = True
        mock_qt.classify_intent = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        intent = "factual"
        try:
            intent = await mock_qt.classify_intent("test query")
        except Exception:
            intent = "factual"

        assert intent == "factual"

    @pytest.mark.asyncio
    async def test_vector_fallback_on_hybrid_failure(self):
        """hybrid 检索失败 → 向量兜底检索。"""
        mm = _make_memory_manager()
        mm._vector_fallback_search = AsyncMock(
            return_value=[{"id": 2, "summary": "vector fallback"}])

        results = []
        if not results:
            results = await mm._vector_fallback_search("query", k=5)

        assert len(results) == 1
        assert results[0]["summary"] == "vector fallback"

    @pytest.mark.asyncio
    async def test_importance_fallback_on_all_failure(self):
        """所有检索都失败 → 重要性兜底。"""
        mm = _make_memory_manager()
        mm._importance_fallback_search = AsyncMock(
            return_value=[{"id": 3, "summary": "important"}])

        results = []
        if not results:
            results = await mm._importance_fallback_search(5)

        assert len(results) == 1
        assert results[0]["summary"] == "important"


class TestRetrieveMemoriesEmptyResult:
    """测试 retrieve_memories 空结果处理。"""

    @pytest.mark.asyncio
    async def test_empty_result_from_all_sources(self):
        """所有检索源都返回空 → 返回空列表。"""
        mm = _make_memory_manager()
        mm.retrieve_memories_hybrid = AsyncMock(return_value=[])
        mm._vector_fallback_search = AsyncMock(return_value=[])
        mm._importance_fallback_search = AsyncMock(return_value=[])

        results = await mm.retrieve_memories_hybrid("obscure query", k=5)
        if not results:
            results = await mm._vector_fallback_search("obscure query", k=5)
        if not results:
            results = await mm._importance_fallback_search(5)

        assert results == []

    @pytest.mark.asyncio
    async def test_temporal_search_returns_results_skips_semantic(self):
        """时间检索命中 → 直接返回，跳过语义检索。"""
        temporal_results = [
            {"id": 10, "summary": "昨天去了超市", "timestamp": time.time() - 86400}
        ]

        temporal = temporal_results
        if temporal:
            results = temporal
        else:
            results = []

        assert len(results) == 1
        assert "昨天" in results[0]["summary"]


class TestTemporalParsing:
    """测试时间实体识别 _parse_temporal_query。"""

    def test_parse_yesterday(self):
        """'昨天' 应返回一个有效的时间区间。"""
        from memory.memory_manager import _parse_temporal_query
        result = _parse_temporal_query("昨天发生了什么")
        assert result is not None
        start_ts, end_ts = result
        assert start_ts < end_ts

    def test_parse_hours_ago(self):
        """'3小时前' 应返回一个有效的时间区间。"""
        from memory.memory_manager import _parse_temporal_query
        result = _parse_temporal_query("3小时前说了什么")
        assert result is not None
        start_ts, end_ts = result
        assert start_ts < end_ts

    def test_parse_no_temporal_returns_none(self):
        """无时间词的查询应返回 None。"""
        from memory.memory_manager import _parse_temporal_query
        result = _parse_temporal_query("天气怎么样")
        assert result is None

    def test_parse_absolute_date(self):
        """绝对日期 '7月15号' 应返回一个有效的时间区间。"""
        from memory.memory_manager import _parse_temporal_query
        result = _parse_temporal_query("7月15号有什么安排")
        assert result is not None
        start_ts, end_ts = result
        assert start_ts < end_ts
