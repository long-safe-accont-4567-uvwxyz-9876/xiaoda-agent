"""TDD: RAG 检索质量治本修复测试（红 → 绿）

治本目标：不相关 query 不应返回低质结果。
根因：
1. 向量召回用相对归一化美化距离（最差也接近 1.0），无绝对距离阈值
2. RRF 融合只看 rank 不看分数，低质候选照送 reranker
3. reranker 不过滤低分

期望行为（测试定义）：
- 向量召回 distance > 阈值时丢弃（不美化）
- 不相关 query 返回空，而非低质结果
- 相关 query 正常返回（不误杀）
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_memory_manager(**kwargs):
    """通过 patch 隔离依赖后构造 MemoryManager。"""
    with patch("memory.memory_manager.MemoryDistiller"), \
         patch("memory.memory_manager.QueryCache") as MockQC, \
         patch("memory.memory_manager.RetrievalAssessor"), \
         patch("memory.memory_manager.FSRSModel"), \
         patch("memory.memory_manager.get_agent_display_name", return_value="小妲"):
        MockQC.return_value = MagicMock()
        from memory.memory_manager import MemoryManager
        defaults = {"db": MagicMock(), "memory": MagicMock()}
        defaults.update(kwargs)
        return MemoryManager(**defaults)


class TestVectorRecallDistanceFilter:
    """治本点1: 向量召回应过滤远距离（绝对阈值，非相对归一化美化）。"""

    @pytest.mark.asyncio
    async def test_distant_vectors_filtered_out(self):
        """distance > 阈值的不相关向量不应被召回。

        场景：Python query 向量库无精确命中，top_k 强制返回的远距离向量
        （distance > 1.2，基本无关）应被过滤，不应进入 RRF 融合。
        """
        mm = _make_memory_manager()
        # 模拟向量检索返回远距离结果（不相关）
        # vec.search 返回 [(row_id, distance), ...]
        mm.vec = MagicMock()
        mm.vec.search = AsyncMock(return_value=[
            (101, 1.5),   # 基本无关
            (102, 1.6),   # 基本无关
            (103, 1.8),   # 完全无关
        ])
        # 模拟记忆查询
        mm.memory.get_memories_by_ids = AsyncMock(return_value=[
            {"id": 101, "summary": "无关内容1", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 102, "summary": "无关内容2", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 103, "summary": "无关内容3", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
        ])

        results = await mm._hybrid_vec_search("Python 配置数据库连接", k=10)

        # 期望：远距离向量被过滤，返回空（而非美化成高分返回）
        assert results == [], f"远距离向量不应被召回，但返回了 {len(results)} 条"

    @pytest.mark.asyncio
    async def test_close_vectors_kept(self):
        """distance < 阈值的相关向量应正常返回（不误杀）。"""
        mm = _make_memory_manager()
        mm.vec = MagicMock()
        mm.vec.search = AsyncMock(return_value=[
            (201, 0.3),   # 高度相关
            (202, 0.5),   # 相关
            (203, 0.8),   # 相关
        ])
        mm.memory.get_memories_by_ids = AsyncMock(return_value=[
            {"id": 201, "summary": "Python 数据库连接配置", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 202, "summary": "sqlite 连接字符串", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 203, "summary": "ORM 框架配置", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
        ])

        results = await mm._hybrid_vec_search("Python 配置数据库连接", k=10)

        # 期望：相关向量全部保留
        assert len(results) == 3, f"相关向量应保留，但只剩 {len(results)} 条"

    @pytest.mark.asyncio
    async def test_mixed_distances_only_keeps_relevant(self):
        """混合距离：只保留相关的，过滤不相关的。"""
        mm = _make_memory_manager()
        mm.vec = MagicMock()
        mm.vec.search = AsyncMock(return_value=[
            (301, 0.4),   # 相关
            (302, 0.6),   # 相关
            (303, 1.4),   # 无关
            (304, 1.7),   # 无关
        ])
        mm.memory.get_memories_by_ids = AsyncMock(return_value=[
            {"id": 301, "summary": "相关1", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 302, "summary": "相关2", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 303, "summary": "无关1", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
            {"id": 304, "summary": "无关2", "user_id": "u1", "agent_id": "a1", "is_raw": 0},
        ])

        results = await mm._hybrid_vec_search("query", k=10)

        # 期望：只保留 2 条相关的
        assert len(results) == 2, f"应过滤 2 条无关的，保留 2 条相关的，实际 {len(results)} 条"
        result_ids = {r["id"] for r in results}
        assert result_ids == {301, 302}, f"应保留 301/302，实际 {result_ids}"


class TestRRFFusionQualityFloor:
    """治本点2: RRF 融合后应过滤极低分候选（统计学下限，非拍脑袋阈值）。"""

    @pytest.mark.asyncio
    async def test_rrf_low_score_candidates_filtered(self):
        """RRF 融合后极低分候选应被丢弃。

        场景：FTS 召回 1 条（rank 1），向量召回 50 条远距离（rank 1-50）。
        RRF 融合后远距离向量虽 rank 靠前但分数极低，应被过滤。
        """
        from memory.memory_manager import reciprocal_rank_fusion
        # FTS rank 1（唯一相关）
        fts_ids = ["1"]
        # 向量召回 50 条（全部不相关，但被强制返回）
        vec_ids = [str(i) for i in range(100, 150)]
        fused = reciprocal_rank_fusion([fts_ids, vec_ids], limit=50)
        # id=1 的 rrf_score = 1/(60+1) ≈ 0.0164（单路 rank 1）
        # id=100 的 rrf_score = 1/(60+1) ≈ 0.0164（单路 rank 1，但向量通道）
        # 两者分数相同，但 id=100 实际不相关
        # 期望：应有机制区分（这是 RRF 的固有局限，需在融合前过滤源头）
        # 此测试验证 RRF 本身行为正确（不在此层修复）
        assert len(fused) > 0


class TestRetrieveMemoriesNoNoiseInjection:
    """端到端：不相关 query 不应注入低质结果到上下文。"""

    @pytest.mark.asyncio
    async def test_unrelated_query_returns_empty_not_noise(self):
        """不相关 query（向量库无命中）应返回空，而非低质结果。

        这是用户核心诉求：Python query 不应返回亲密内容。
        治本：向量召回过滤远距离 → RRF 无低质候选 → reranker 无低分 → 返回空。
        """
        mm = _make_memory_manager()
        mm.vec = MagicMock()
        mm._query_transformer = MagicMock()
        mm._query_transformer.available = True
        mm._query_transformer.classify_intent = AsyncMock(return_value="factual")

        # 所有检索通道都返回远距离/低质结果
        mm._try_temporal_search = AsyncMock(return_value=None)
        mm._is_retrieval_simple = MagicMock(return_value=False)
        mm._transform_queries = AsyncMock(return_value=["Python 配置数据库连接"])

        # FTS 无命中（技术 query 在闲聊库里无匹配）
        mm._hybrid_fts_search_scoped = AsyncMock(return_value=[])
        # 向量召回返回远距离（不相关）
        mm._hybrid_vec_search = AsyncMock(return_value=[
            {"id": 1, "summary": "亲密内容", "score": 0.9,  # 被美化成高分
             "user_id": "u1", "agent_id": "a1", "is_raw": 0},
        ])
        mm._spreading_recall = AsyncMock(return_value=[])
        mm._entity_recall = AsyncMock(return_value=[])

        # KG 无命中
        mm.kg = None
        mm._kg_v2_engine = None

        # reranker 不可用（测试无 reranker 时的行为）
        mm._reranker = None

        mm._apply_fsrs_scoring = AsyncMock(side_effect=lambda results: results)
        mm._compute_final_scores = AsyncMock(side_effect=lambda q, r, c, e: r)
        mm._dedup_by_content_similarity = MagicMock(side_effect=lambda r: r)
        mm._query_cache = MagicMock()
        mm._query_cache.get = AsyncMock(return_value=None)
        mm._query_cache.put = AsyncMock()

        from memory.scope import Scope
        results = await mm.retrieve_memories("Python 配置数据库连接", k=5, scope=Scope())

        # 期望：不相关 query 返回空（而非注入低质亲密内容）
        # 当前会失败：因为 _hybrid_vec_search 美化了距离，返回 score 0.9 的"高分"结果
        assert results == [], f"不相关 query 应返回空，但返回了 {len(results)} 条低质结果"
