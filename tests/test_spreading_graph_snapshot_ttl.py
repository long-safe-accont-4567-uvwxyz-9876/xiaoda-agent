"""整图边快照 TTL 复用 + 后台重建测试。

背景（2026-08-21）：快照实测 2381 节点 / 100 万边，重建 3-10s。
原实现每个记忆写入 clear_cache() 都作废快照 → 每轮对话检索都重建 →
每条消息固定多付 6-10s。修复后：
- TTL（GRAPH_SNAPSHOT_TTL）内复用，零等待
- 过期：后台重建，本次返回旧快照不阻塞
- 冷启动（无快照）：内联等待
- clear_cache() 不再作废图快照（只清 recall 缓存）
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.spreading_activation import SpreadingActivationEngine


def _make_engine(snapshot: dict | None = None) -> SpreadingActivationEngine:
    db = MagicMock()
    db.get_edge_snapshot = AsyncMock(return_value=snapshot or {"a": {"b": 1.0}})
    eng = SpreadingActivationEngine(
        concept_db=db, vector_store=None,
        key_extractor=MagicMock(extract=MagicMock(return_value={"k"})),
    )
    return eng


@pytest.mark.asyncio
async def test_snapshot_reused_within_ttl():
    """TTL 内连续调用只重建一次（零等待）。"""
    eng = _make_engine()
    s1 = await eng._ensure_graph_snapshot()
    s2 = await eng._ensure_graph_snapshot()
    assert s1 is s2
    assert eng.db.get_edge_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_clear_cache_keeps_graph_snapshot():
    """记忆写入（clear_cache）不再作废图快照。"""
    eng = _make_engine()
    s1 = await eng._ensure_graph_snapshot()
    eng.clear_cache()          # 只清 recall 缓存
    eng.clear_cache()
    s2 = await eng._ensure_graph_snapshot()
    assert s1 is s2
    assert eng.db.get_edge_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_expired_snapshot_rebuilds_in_background():
    """TTL 过期：立即返回旧快照（阻塞），后台重建并更新。"""
    eng = _make_engine({"old": {"x": 0.5}})
    s1 = await eng._ensure_graph_snapshot()
    # 人为过期
    eng._graph_ts -= eng.GRAPH_SNAPSHOT_TTL + 1
    # 第二次调用：返回旧快照，同时后台触发重建
    s2 = await eng._ensure_graph_snapshot()
    assert s2 is s1  # 未阻塞等新快照
    assert eng._graph_build_task is not None
# 等后台重建完成
    await eng._graph_build_task
    assert eng.db.get_edge_snapshot.await_count == 2
    # 重建完成后下一次调用不再触发重建
    await eng._ensure_graph_snapshot()
    assert eng.db.get_edge_snapshot.await_count == 2


@pytest.mark.asyncio
async def test_cold_start_builds_inline():
    """冷启动（无快照）：等待重建完成。"""
    eng = _make_engine({"a": {"b": 1.0}})
    s = await eng._ensure_graph_snapshot()
    assert s == {"a": {"b": 1.0}}
    assert eng.db.get_edge_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_cold_start_share_one_build():
    """并发冷启动共享同一个重建任务（只调一次 get_edge_snapshot）。"""

    eng = _make_engine({"a": {"b": 1.0}})

    async def slow_build():
        await asyncio.sleep(0.05)
        return {"fresh": {"b": 2.0}}

    eng.db.get_edge_snapshot = AsyncMock(side_effect=slow_build)

    r1, r2 = await asyncio.gather(
        eng._ensure_graph_snapshot(),
        eng._ensure_graph_snapshot(),
    )
    assert r1 == r2 == {"fresh": {"b": 2.0}}
    assert eng.db.get_edge_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_spreading_channel_tolerates_none_snapshot():
    """快照为 None 时 _spreading_channel 不崩（graph or {} 兜底）。"""
    eng = _make_engine()
    eng._graph_snapshot = None
    result = await eng._spreading_channel(
        direct={"a": 1.0}, alive_nodes={"a": {"text": "x"}}
    )
    assert isinstance(result, dict)
