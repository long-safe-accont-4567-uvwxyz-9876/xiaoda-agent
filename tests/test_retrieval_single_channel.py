"""B1 重构契约测试：_resolve_fallback_or_single_channel 数据驱动改造。

原实现 7 个 if 块各自重复「检查单路有结果 → 补 rrf_score → 切片 k →
可选追 kg_v2 → log → return」，重构为数据驱动表查 + 统一辅助函数。
契约：7 种单路场景与全空/全多路场景的返回值完全一致。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _make_engine():
    """构造 RetrievalEngine，mm 用 MagicMock。"""
    from memory._retrieval_engine import RetrievalEngine
    mm = MagicMock()
    mm._governance = None
    mm._memory_count_cache = None
    mm._memory_count_ts = 0
    mm.memory = MagicMock()
    mm.memory.get_episodic_count = AsyncMock(return_value=0)
    return RetrievalEngine(mm)


async def _call_single_channel(channel_key, items, kg_v2_items=None, k=5):
    """调用 _resolve_fallback_or_single_channel，只让 channel_key 这一路有结果。"""
    eng = _make_engine()
    from memory._retrieval_engine import RecallChannels
    empty: list = []
    d = {
        "fts": items if channel_key == "fts" else list(empty),
        "vec": items if channel_key == "vec" else list(empty),
        "kg": items if channel_key == "kg" else list(empty),
        "child": items if channel_key == "child" else list(empty),
        "spread": items if channel_key == "spread" else list(empty),
        "entity": items if channel_key == "entity" else list(empty),
    }
    channels = RecallChannels(
        d["fts"], d["vec"], d["kg"], d["child"],
        d["spread"], d["entity"], kg_v2_items or [],
    )
    return await eng._resolve_fallback_or_single_channel(
        channels, query="test", k=k, tier="warm", _start=0,
        candidate_ids=None, recall_limit=50, scope=None, query_vec=None,
    )


@pytest.mark.parametrize("channel,score_field,score_val", [
    ("fts", "score", 0.9),
    ("vec", "similarity", 0.85),
    ("kg", "score", 0.8),
    ("child", "score", 0.75),
    ("spread", "spreading_score", 0.7),
    ("entity", "score", 0.65),
])
@pytest.mark.asyncio
async def test_single_channel_sets_rrf_score(channel, score_field, score_val):
    """单路有结果时，每条记录应被补上 rrf_score（取自该路的 score 字段）。"""
    items = [{"id": i, score_field: score_val, "summary": f"item {i}"} for i in range(3)]
    results = await _call_single_channel(channel, items, k=5)
    assert results is not None
    assert len(results) == 3
    for r in results:
        assert "rrf_score" in r
        assert r["rrf_score"] == score_val


@pytest.mark.asyncio
async def test_single_channel_truncates_to_k():
    """单路结果超过 k 条时截断到 k。"""
    items = [{"id": i, "score": 0.9, "summary": f"item {i}"} for i in range(10)]
    results = await _call_single_channel("fts", items, k=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_single_channel_appends_kg_v2_when_below_k():
    """单路结果不足 k 条且有 kg_v2 时，追加 kg_v2 补足。"""
    items = [{"id": i, "score": 0.9, "summary": f"item {i}"} for i in range(2)]
    kg_v2 = [{"summary": "kg fact", "rrf_score": 0.5, "source": "kg_v2"}]
    results = await _call_single_channel("fts", items, kg_v2_items=kg_v2, k=5)
    assert len(results) == 3  # 2 原始 + 1 kg_v2
    assert results[-1]["source"] == "kg_v2"


@pytest.mark.asyncio
async def test_single_channel_no_append_kg_v2_when_full():
    """单路结果已满 k 时不追加 kg_v2。"""
    items = [{"id": i, "score": 0.9, "summary": f"item {i}"} for i in range(5)]
    kg_v2 = [{"summary": "kg fact", "rrf_score": 0.5, "source": "kg_v2"}]
    results = await _call_single_channel("fts", items, kg_v2_items=kg_v2, k=5)
    assert len(results) == 5
    assert all(r.get("source") != "kg_v2" for r in results)


@pytest.mark.asyncio
async def test_all_empty_returns_empty_list():
    """七路全空（且无 fallback 命中）时返回 []，不是 None。"""
    eng = _make_engine()
    from memory._retrieval_engine import RecallChannels
    eng._mm._hybrid_fts_search_scoped = AsyncMock(return_value=[])
    eng._mm._hybrid_vec_search = AsyncMock(return_value=[])
    result = await eng._resolve_fallback_or_single_channel(
        RecallChannels([], [], [], [], [], [], []),
        query="test", k=5, tier="warm", _start=0,
        candidate_ids=None, recall_limit=50, scope=None, query_vec=None,
    )
    assert result == []


@pytest.mark.asyncio
async def test_multi_channel_returns_none():
    """多路有结果时返回 None（交给 RRF 融合）。"""
    eng = _make_engine()
    from memory._retrieval_engine import RecallChannels
    fts = [{"id": 1, "score": 0.9, "summary": "fts"}]
    vec = [{"id": 2, "similarity": 0.8, "summary": "vec"}]
    result = await eng._resolve_fallback_or_single_channel(
        RecallChannels(fts, vec, [], [], [], [], []),
        query="test", k=5, tier="warm", _start=0,
        candidate_ids=None, recall_limit=50, scope=None, query_vec=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_kg_v2_only_returns_directly():
    """仅 KG v2 有结果时直接返回（不补 rrf_score）。"""
    eng = _make_engine()
    from memory._retrieval_engine import RecallChannels
    kg_v2 = [{"summary": "fact1", "rrf_score": 0.5, "source": "kg_v2"},
             {"summary": "fact2", "rrf_score": 0.3, "source": "kg_v2"}]
    result = await eng._resolve_fallback_or_single_channel(
        RecallChannels([], [], [], [], [], [], kg_v2),
        query="test", k=5, tier="warm", _start=0,
        candidate_ids=None, recall_limit=50, scope=None, query_vec=None,
    )
    assert result is not None
    assert len(result) == 2
    assert result[0]["rrf_score"] == 0.5
