"""I6: KG 参与 RAG 检索 — 单元测试

覆盖:
- db_memory.search_memories_by_entities (KG 召回的 DB 反查)
- knowledge_graph.get_relevance_boost_fast (复用已存储 entities, 避免 N+1 LLM)
- knowledge_graph.recall_by_entities (KG 关联实体召回)
- knowledge_graph.get_query_entities (query 实体提取)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# search_memories_by_entities
# ============================================================

@pytest.mark.asyncio
async def test_search_memories_by_entities_hit():
    """按实体反查应命中带该实体标签的记忆"""
    import aiosqlite
    from db.db_memory import MemoryDB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("""
                CREATE TABLE episodic_memories (
                    id TEXT PRIMARY KEY,
                    summary TEXT,
                    importance REAL DEFAULT 0.5,
                    timestamp REAL,
                    session_id TEXT DEFAULT 'user',
                    entities TEXT
                )
            """)
            await conn.execute(
                "INSERT INTO episodic_memories (id, summary, importance, timestamp, entities) "
                "VALUES (?, ?, ?, ?, ?)",
                ("m1", "和小妲去公园", 0.8, time.time(),
                 json.dumps(["小妲", "公园"], ensure_ascii=False))
            )
            await conn.execute(
                "INSERT INTO episodic_memories (id, summary, importance, timestamp, entities) "
                "VALUES (?, ?, ?, ?, ?)",
                ("m2", "吃苹果", 0.6, time.time(),
                 json.dumps(["苹果"], ensure_ascii=False))
            )
            await conn.commit()
            db = MemoryDB(conn)
            results = await db.search_memories_by_entities(["小妲"], limit=5)
            assert len(results) == 1
            assert results[0]["id"] == "m1"
            assert "小妲" in results[0]["entity_list"]
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_search_memories_by_entities_empty():
    """空实体列表应返回空结果"""
    import aiosqlite
    from db.db_memory import MemoryDB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE episodic_memories (id TEXT, entities TEXT)")
            await conn.commit()
            db = MemoryDB(conn)
            results = await db.search_memories_by_entities([], limit=5)
            assert results == []
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_search_memories_by_entities_excludes_archived():
    """已归档记忆不应被召回"""
    import aiosqlite
    from db.db_memory import MemoryDB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("""
                CREATE TABLE episodic_memories (
                    id TEXT PRIMARY KEY, summary TEXT, importance REAL,
                    timestamp REAL, session_id TEXT, entities TEXT
                )
            """)
            await conn.execute(
                "INSERT INTO episodic_memories VALUES (?, ?, ?, ?, ?, ?)",
                ("active", "活跃记忆", 0.8, time.time(), "user",
                 json.dumps(["猫"], ensure_ascii=False))
            )
            await conn.execute(
                "INSERT INTO episodic_memories VALUES (?, ?, ?, ?, ?, ?)",
                ("archived", "归档记忆", 0.9, time.time(), "archived",
                 json.dumps(["猫"], ensure_ascii=False))
            )
            await conn.commit()
            db = MemoryDB(conn)
            results = await db.search_memories_by_entities(["猫"], limit=5)
            assert len(results) == 1
            assert results[0]["id"] == "active"
    finally:
        os.unlink(path)


# ============================================================
# get_relevance_boost_fast
# ============================================================

@pytest.mark.asyncio
async def test_get_relevance_boost_fast_overlap():
    """query 实体与记忆实体有重叠时 boost > 0"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    query_entities = {"小妲", "公园"}
    memory_entities_list = [["小妲", "吃饭"], ["苹果"]]
    boosts = await kg.get_relevance_boost_fast(query_entities, memory_entities_list)
    assert len(boosts) == 2
    assert boosts[0] > 0  # "小妲" 重叠
    assert boosts[1] == 0.0  # 无重叠


@pytest.mark.asyncio
async def test_get_relevance_boost_fast_cap():
    """boost 应被截断到 0.5"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    # 很多重叠实体
    query_entities = {f"e{i}" for i in range(10)}
    memory_entities_list = [list(query_entities)]
    boosts = await kg.get_relevance_boost_fast(query_entities, memory_entities_list)
    assert boosts[0] <= 0.5


@pytest.mark.asyncio
async def test_get_relevance_boost_fast_no_llm():
    """快速评分不应触发 LLM (extract_from_summary 不被调用)"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    with patch.object(kg, "extract_from_summary", new=AsyncMock()) as mock:
        await kg.get_relevance_boost_fast({"猫"}, [["猫"], ["狗"]])
        mock.assert_not_called()


# ============================================================
# recall_by_entities
# ============================================================

@pytest.mark.asyncio
async def test_recall_by_entities_returns_related():
    """KG 召回应返回关联实体（排除 query 自身实体）"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    kg.knowledge_db = object()  # 非 None 即可, 实际查询被 mock

    async def fake_get_related(entity_names, depth=1):
        items = []
        for name in entity_names:
            items.append({"type": "entity", "data": {"name": name}})
            items.append({"type": "entity", "data": {"name": name + "_相关"}})
            items.append({"type": "relation",
                          "data": {"from_entity": name, "relation_type": "位于",
                                   "to_entity": "北京"}})
        return items

    with patch.object(kg, "get_related_knowledge", side_effect=fake_get_related):
        related = await kg.recall_by_entities({"小妲"}, limit=5)
    # 应包含 "小妲_相关" 和 "北京", 但不包含 "小妲" 自身
    assert "小妲_相关" in related
    assert "北京" in related
    assert "小妲" not in related


@pytest.mark.asyncio
async def test_recall_by_entities_empty_query():
    """空 query 实体应返回空列表"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    related = await kg.recall_by_entities(set(), limit=5)
    assert related == []


@pytest.mark.asyncio
async def test_recall_by_entities_no_db():
    """无 knowledge_db 时应返回空列表"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    kg.knowledge_db = None
    related = await kg.recall_by_entities({"小妲"}, limit=5)
    assert related == []


# ============================================================
# get_query_entities
# ============================================================

@pytest.mark.asyncio
async def test_get_query_entities_extracts():
    """应从 query 提取实体名"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    with patch.object(kg, "extract_from_summary", new=AsyncMock(
            return_value={"entities": [{"name": "小妲"}, {"name": "公园"}]})):
        entities = await kg.get_query_entities("小妲去公园")
    assert entities == {"小妲", "公园"}


@pytest.mark.asyncio
async def test_get_query_entities_failure_returns_empty():
    """LLM 失败时应返回空集合（不抛异常）"""
    from memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    with patch.object(kg, "extract_from_summary", new=AsyncMock(
            side_effect=RuntimeError("LLM down"))):
        entities = await kg.get_query_entities("test")
    assert entities == set()
