"""KG v2 向量存储 + 混合检索测试。"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.scope import Scope, ScopeBoundary


@pytest.fixture
def mock_vec_store():
    """创建带 mock embed 的 VectorStore。"""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite_vec not available")
    from memory.vector_store import VectorStore
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # 显式 remote：避免测试继承 .env 的 EMBED_MODE=local（本地模型 512 维
    # 与测试 mock 的 1024 维向量不匹配导致 upsert 失败），隔离环境副作用
    store = VectorStore(path, embed_api_key="fake-key", embed_mode="remote")
    return store, path


@pytest.mark.asyncio
async def test_relation_search_uses_episode_ref_table_not_cached_json(tmp_path):
    manager = DatabaseManager(tmp_path / "provenance.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_episode("EP-real", "用户喜欢篮球", "summary", 1000.0, time.time())
    await db.insert_relation_v2(
        "REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-real", 1000.0
    )
    await manager._conn.execute(
        "UPDATE kg_relations_v2 SET episode_ids='[\"EP-stale\"]' WHERE id='REL-1'"
    )
    await manager._conn.commit()

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search("篮球", top_k=5)
    relation = next(row for row in results if row.get("id") == "REL-1")

    assert json.loads(relation["episode_ids"]) == ["EP-real"]
    await manager.close()


@pytest.mark.asyncio
async def test_kg_vec_tables_created(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        # Verify tables exist
        import sqlite3
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "kg_entities_vec" in tables
        assert "kg_relations_vec" in tables
    finally:
        await store.close()
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_upsert_and_search_kg_entity(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        # Mock embed to return deterministic vector
        store._cache.clear() if hasattr(store._cache, 'clear') else None
        store.embed = AsyncMock(return_value=[[0.1] * 1024])

        ok = await store.upsert_kg_entity(1, "篮球: 团队运动")
        assert ok is True

        results = await store.search_kg_entities("团队运动", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # rowid
    finally:
        await store.close()
        import os
        os.unlink(path)


@pytest.mark.asyncio
async def test_upsert_and_search_kg_relation(mock_vec_store):
    store, path = mock_vec_store
    try:
        await store.init()
        store.embed = AsyncMock(return_value=[[0.2] * 1024])

        ok = await store.upsert_kg_relation(1, "用户喜欢打篮球")
        assert ok is True

        results = await store.search_kg_relations("篮球", top_k=5)
        assert len(results) >= 1
        assert results[0][0] == 1  # rowid
    finally:
        await store.close()
        import os
        os.unlink(path)


# ── KGSearchEngine tests ──────────────────────────────────────

from memory.kg_search import KGSearchEngine


def test_rrf_fuse_combines_ranked_lists():
    """RRF 融合: 多路结果按 1/(k+rank) 求和排序。"""
    engine = KGSearchEngine.__new__(KGSearchEngine)
    list_a = [{"type": "entity", "id": "E1"}, {"type": "entity", "id": "E2"}]
    list_b = [{"type": "entity", "id": "E2"}, {"type": "entity", "id": "E3"}]
    fused = engine._rrf_fuse([list_a, list_b], k=60)
    # E2 appears in both lists → highest score
    assert fused[0]["id"] == "E2"
    assert "rrf_score" in fused[0]
    # E1 and E3 appear in one list each
    ids = [f["id"] for f in fused]
    assert "E1" in ids
    assert "E3" in ids


def test_rrf_fuse_empty_lists():
    engine = KGSearchEngine.__new__(KGSearchEngine)
    assert engine._rrf_fuse([], k=60) == []
    assert engine._rrf_fuse([[], []], k=60) == []


@pytest.mark.asyncio
async def test_scoped_semantic_search_passes_relation_candidates_to_vector_store(tmp_path):
    manager = DatabaseManager(tmp_path / "search_scope_vector.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_episode(
        "EP-alice", "Alice 事实", "summary", 1000.0, time.time(),
        group_id="alice::xiaoda",
    )
    await db.insert_relation_v2(
        "REL-alice", "用户", "喜欢", "篮球", "Alice 喜欢篮球", "EP-alice", 1000.0,
    )
    row = await manager._conn.execute_fetchall(
        "SELECT rowid FROM kg_relations_v2 WHERE id = 'REL-alice'"
    )
    alice_rowid = int(row[0][0])
    vector_store = MagicMock()
    vector_store.search_kg_entities = AsyncMock(return_value=[])
    vector_store.search_kg_relations = AsyncMock(
        return_value=[(alice_rowid, 0.2)]
    )

    engine = KGSearchEngine(db=db, vector_store=vector_store, conn=manager._conn)
    results = await engine.search(
        "无全文命中的语义问法", top_k=1,
        scope=Scope(user_id="alice", agent_id="xiaoda"),
    )

    assert [r["id"] for r in results if r.get("type") == "relation"] == [
        "REL-alice"
    ]
    assert vector_store.search_kg_relations.await_args.kwargs["candidate_ids"] == [
        alice_rowid
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_same_user_kg_facts_are_isolated_between_group_sessions(tmp_path):
    manager = DatabaseManager(tmp_path / "search_group_scope.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    group_a = Scope(
        user_id="alice", agent_id="xiaoda", session_id="qq_group:alice:group-a"
    )
    group_b = Scope(
        user_id="alice", agent_id="xiaoda", session_id="qq_group:alice:group-b"
    )
    await db.insert_episode(
        "EP-group-a", "群 A 计划", "summary", 1000.0, time.time(),
        group_id="alice::xiaoda::qq_group:alice:group-a",
    )
    await db.insert_relation_v2(
        "REL-group-a", "群聊", "计划", "露营", "群 A 计划露营",
        "EP-group-a", 1000.0,
    )
    await db.insert_episode(
        "EP-group-b", "群 B 计划", "summary", 1000.0, time.time(),
        group_id="alice::xiaoda::qq_group:alice:group-b",
    )
    await db.insert_relation_v2(
        "REL-group-b", "群聊", "计划", "聚餐", "群 B 计划聚餐",
        "EP-group-b", 1000.0,
    )

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search("群", top_k=10, scope=group_a)

    assert {r["id"] for r in results if r.get("type") == "relation"} == {
        "REL-group-a"
    }
    await manager.close()


@pytest.mark.asyncio
async def test_scoped_fulltext_limit_is_applied_after_scope_filter(tmp_path):
    manager = DatabaseManager(tmp_path / "search_scope_limit.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    for index in range(5):
        episode_id = f"EP-bob-{index}"
        relation_id = f"REL-bob-{index}"
        await db.insert_episode(
            episode_id, "共同关键词", "summary", 1000.0, time.time(),
            group_id="bob::xiaoda",
        )
        await db.insert_relation_v2(
            relation_id, "用户", "提及", str(index), "共同关键词", episode_id, 1000.0,
        )
    await db.insert_episode(
        "EP-alice", "共同关键词", "summary", 1000.0, time.time(),
        group_id="alice::xiaoda",
    )
    await db.insert_relation_v2(
        "REL-alice", "用户", "提及", "目标", "共同关键词", "EP-alice", 1000.0,
    )

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search(
        "共同关键词", top_k=1, scope=Scope(user_id="alice", agent_id="xiaoda")
    )

    assert [r["id"] for r in results if r.get("type") == "relation"] == [
        "REL-alice"
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_search_isolates_relation_facts_by_scope(tmp_path):
    manager = DatabaseManager(tmp_path / "search_scope.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    alice_scope = Scope(user_id="alice", agent_id="xiaoda")
    bob_scope = Scope(user_id="bob", agent_id="xiaoda")

    await db.insert_episode(
        "EP-alice", "用户喜欢篮球", "summary", 1000.0, time.time(),
        group_id="alice::xiaoda",
    )
    await db.insert_relation_v2(
        "REL-alice", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-alice", 1000.0,
    )
    await db.insert_episode(
        "EP-bob", "用户喜欢网球", "summary", 1000.0, time.time(),
        group_id="bob::xiaoda",
    )
    await db.insert_relation_v2(
        "REL-bob", "用户", "喜欢", "网球", "用户喜欢网球", "EP-bob", 1000.0,
    )

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    alice_results = await engine.search("用户喜欢", top_k=10, scope=alice_scope)
    bob_results = await engine.search("用户喜欢", top_k=10, scope=bob_scope)

    assert {r["id"] for r in alice_results if r.get("type") == "relation"} == {
        "REL-alice"
    }
    assert {r["id"] for r in bob_results if r.get("type") == "relation"} == {
        "REL-bob"
    }
    await manager.close()


@pytest.mark.asyncio
async def test_search_returns_current_facts_only_by_default(tmp_path):
    """默认只返回当前有效事实 (is_current=1)。"""
    manager = DatabaseManager(tmp_path / "search_curr.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    # Insert data
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "用户是人物")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "篮球是运动")
    await db.insert_episode("EP-1", "用户喜欢篮球", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    # Insert invalidated relation
    await db.insert_episode("EP-2", "用户改打网球", "summary", 2000.0, time.time())
    await db.insert_entity_v2("ENT-t", "网球", "概念", [], "网球是运动")
    await db.insert_relation_v2("REL-2", "用户", "喜欢", "网球", "用户喜欢网球", "EP-2", 2000.0)
    await db.invalidate_relation("REL-1", invalid_at=2000.0, expired_at=2001.0)

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search("篮球", top_k=10)
    # REL-1 is invalidated → should not appear
    rel_ids = [r["id"] for r in results if r.get("type") == "relation"]
    assert "REL-1" not in rel_ids
    relation_rows = [r for r in results if r.get("type") == "relation"]
    assert all("episode_ids" in row for row in relation_rows)
    await manager.close()


@pytest.mark.asyncio
async def test_search_with_as_of_returns_historical_snapshot(tmp_path):
    """as_of 时间戳返回历史快照。"""
    manager = DatabaseManager(tmp_path / "search_asof.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_episode("EP-1", "用户喜欢篮球", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    await db.invalidate_relation("REL-1", invalid_at=2000.0, expired_at=2001.0)

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    # as_of=1500 → REL-1 was still valid
    results = await engine.search("篮球", top_k=10, as_of=1500.0)
    rel_ids = [r["id"] for r in results if r.get("type") == "relation"]
    assert "REL-1" in rel_ids
    await manager.close()


@pytest.mark.asyncio
async def test_fulltext_search_finds_entities_by_summary(tmp_path):
    """FTS5 全文搜索实体。"""
    manager = DatabaseManager(tmp_path / "fts_search.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-1", "篮球", "概念", [], "团队运动，需要五个球员")
    await db.insert_entity_v2("ENT-2", "足球", "概念", [], "另一种运动")

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine._fulltext_search("篮球", k=5)
    ids = [r["id"] for r in results]
    assert "ENT-1" in ids
    await manager.close()


@pytest.mark.asyncio
async def test_graph_search_finds_neighbors(tmp_path):
    """图遍历搜索找到邻居实体。"""
    manager = DatabaseManager(tmp_path / "graph.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_entity_v2("ENT-t", "网球", "概念", [], "")
    await db.insert_episode("EP-1", "", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    await db.insert_episode("EP-2", "", "summary", 2000.0, time.time())
    await db.insert_relation_v2("REL-2", "用户", "喜欢", "网球", "用户喜欢网球", "EP-2", 2000.0)

    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    # Mock entity extraction from query
    engine._extract_query_entities = AsyncMock(return_value={"用户"})
    results = await engine._graph_search("用户", k=5)
    names = [r["id"] for r in results]
    assert "篮球" in names or "网球" in names
    await manager.close()

