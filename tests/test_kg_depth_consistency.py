"""KG get_related_knowledge 实体/关系一致性守卫（2026-08-25 扩围 review 发现）。

原实现：BFS 深度耗尽时 frontier 邻居只进 visited，不进 all_entities——
返回的 relations 引用了 entities 里不存在的实体（悬空引用），下游
KnowledgeGraph.get_related_knowledge 把两者平铺注入 prompt，
LLM 会看到"关系提到 A-B 但实体列表没有 B"的不一致信息。

修复：depth 耗尽后对 dangling 邻居补查一轮实体表。
不变式（本文件钉死）：**relations 出现的每个端点都必须在 entities 中**。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


class _RealKG:
    """DDL + KnowledgeDB 组合（与生产同构的最小真实现）。"""

    from db.ddl_schema import DDLMixin  # noqa: I001 —— 组合 mixin 需类体执行
    from db.db_knowledge import KnowledgeDB


from db.db_knowledge import KnowledgeDB  # noqa: E402
from db.ddl_schema import DDLMixin  # noqa: E402


class _RealKGImpl(DDLMixin, KnowledgeDB):
    pass


async def _make_graph():
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    db = _RealKGImpl(conn)
    await db._ddl_knowledge_tables()
    for name in ("A", "B", "C", "D", "E", "F"):
        await db.upsert_knowledge_entity(name, kind="测试")
    for rid, a, rt, b in [
        ("r1", "A", "相邻", "B"),
        ("r2", "B", "相邻", "C"),
        ("r3", "C", "相邻", "D"),
        ("r4", "A", "引用", "F"),
        ("r5", "F", "反向引用", "A"),
    ]:
        await db.insert_knowledge_relation(rid, a, rt, b)
    return conn, db


def _assert_no_dangling(result: dict) -> None:
    """核心不变式：relations 的每个端点 ⊆ entities。"""
    ents = {e["name"] for e in result["entities"]}
    for r in result["relations"]:
        assert r["from_entity"] in ents, f"悬空引用: from={r['from_entity']} 不在实体集"
        assert r["to_entity"] in ents, f"悬空引用: to={r['to_entity']} 不在实体集"


@pytest.mark.asyncio
async def test_depth1_neighbors_are_included_not_dangling():
    """depth=1 时邻居实体必须出现在 entities（修复前只有 A 自己）。"""
    conn, db = await _make_graph()
    try:
        r = await db.get_related_knowledge(["A"], depth=1)
        names = {e["name"] for e in r["entities"]}
        assert names == {"A", "B", "F"}, f"单跳语义破坏: {names}"
        _assert_no_dangling(r)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_depth_expansion_and_exclusion():
    """多跳按深度扩展;无关连通分量(E)永不混入。"""
    conn, db = await _make_graph()
    try:
        r2 = await db.get_related_knowledge(["A"], depth=2)
        n2 = {e["name"] for e in r2["entities"]}
        assert {"A", "B", "C", "F"} <= n2 and "D" not in n2 and "E" not in n2
        _assert_no_dangling(r2)

        r3 = await db.get_related_knowledge(["A"], depth=3)
        n3 = {e["name"] for e in r3["entities"]}
        assert "D" in n3 and "E" not in n3
        _assert_no_dangling(r3)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_empty_and_missing_entities_stay_monotonic():
    """空实体列表 / 不存在的实体 → 单调空结果,不炸。"""
    conn, db = await _make_graph()
    try:
        assert await db.get_related_knowledge([], depth=1) == {
            "entities": [], "relations": []}
        rx = await db.get_related_knowledge(["不存在"], depth=2)
        assert rx["entities"] == [] and rx["relations"] == []
    finally:
        await conn.close()
