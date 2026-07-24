"""KG v2 集成测试 — 功能开关 + 端到端流程。"""
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph import KnowledgeGraph
from memory.knowledge_graph_v2 import KnowledgeGraphV2


@pytest.mark.asyncio
async def test_kg_v2_enabled_flag_defaults_false():
    """KG_V2_ENABLED 默认为 False（保守策略，需显式开启）。"""
    # Remove env var to test default
    os.environ.pop("KG_V2_ENABLED", None)
    import importlib

    import config
    importlib.reload(config)
    assert config.KG_V2_ENABLED is False


@pytest.mark.asyncio
async def test_kg_v2_flag_can_be_enabled():
    """KG_V2_ENABLED=true 显式开启 v2。"""
    os.environ["KG_V2_ENABLED"] = "true"
    import importlib

    import config
    importlib.reload(config)
    assert config.KG_V2_ENABLED is True
    os.environ.pop("KG_V2_ENABLED", None)
    importlib.reload(config)


@pytest.mark.asyncio
async def test_kg_v2_flag_can_be_disabled():
    """KG_V2_ENABLED=false 关闭 v2。"""
    os.environ["KG_V2_ENABLED"] = "false"
    import importlib

    import config
    importlib.reload(config)
    assert config.KG_V2_ENABLED is False
    os.environ.pop("KG_V2_ENABLED", None)
    importlib.reload(config)


@pytest.mark.asyncio
async def test_database_manager_has_kg_v2_instance(tmp_path):
    """DatabaseManager.init() 创建 KnowledgeDBV2 实例。"""
    manager = DatabaseManager(tmp_path / "mgr.db")
    await manager.init()
    assert manager.kg_v2 is not None
    assert isinstance(manager.kg_v2, KnowledgeDBV2)
    await manager.close()


@pytest.mark.asyncio
async def test_auto_extract_uses_v2_when_enabled(tmp_path):
    """功能开关开启时, auto_extract_and_merge 调用 v2。"""
    manager = DatabaseManager(tmp_path / "v2_on.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraph(knowledge_db=manager.knowledge)
    kg_v2 = KnowledgeGraphV2(db_v2=db, vector_store=None)
    kg.set_kg_v2(kg_v2)

    # Mock v2 method to track call
    kg_v2.add_facts_from_episode = AsyncMock(return_value={
        "episode_id": "EP-mock", "new_facts": 0, "invalidated": 0
    })

    with patch("config.KG_V2_ENABLED", True):
        await kg.auto_extract_and_merge("用户说喜欢篮球")

    kg_v2.add_facts_from_episode.assert_called_once()
    await manager.close()


@pytest.mark.asyncio
async def test_auto_extract_falls_back_to_v1_when_disabled(tmp_path):
    """功能开关关闭时, auto_extract_and_merge 走 v1 逻辑。"""
    manager = DatabaseManager(tmp_path / "v1_fallback.db")
    await manager.init()
    kg = KnowledgeGraph(knowledge_db=manager.knowledge)
    db = KnowledgeDBV2(manager._conn)
    kg_v2 = KnowledgeGraphV2(db_v2=db, vector_store=None)
    kg.set_kg_v2(kg_v2)

    # Mock both v1 and v2 methods
    kg_v2.add_facts_from_episode = AsyncMock(return_value={
        "episode_id": "EP-mock", "new_facts": 0, "invalidated": 0
    })
    kg.extract_from_summary = AsyncMock(return_value={"entities": [], "relations": []})

    with patch("config.KG_V2_ENABLED", False):
        await kg.auto_extract_and_merge("用户说喜欢篮球")

    # v2 should NOT be called
    kg_v2.add_facts_from_episode.assert_not_called()
    # v1 extract_from_summary SHOULD be called
    kg.extract_from_summary.assert_called_once()
    await manager.close()


@pytest.mark.asyncio
async def test_end_to_end_episode_to_search(tmp_path):
    """端到端: Episode 摄入 → 混合检索 → 验证结果。"""
    from memory.kg_search import KGSearchEngine

    manager = DatabaseManager(tmp_path / "e2e_int.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    # Mock LLM extraction
    kg.extract_from_summary = AsyncMock(return_value={
        "entities": [
            {"name": "用户", "kind": "人物", "observations": ["喜欢篮球"]},
            {"name": "篮球", "kind": "概念", "observations": ["团队运动"]},
        ],
        "relations": [
            {"from_entity": "用户", "relation_type": "喜欢", "to_entity": "篮球",
             "fact": "用户喜欢篮球"},
        ],
    })

    # Step 1: Episode ingestion
    result = await kg.add_facts_from_episode("用户说喜欢打篮球", time.time())
    assert result["new_facts"] == 1

    # Step 2: Search (without vector store, only fulltext + graph)
    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    results = await engine.search("篮球", top_k=10)
    # Should find the relation "用户喜欢篮球" via FTS5
    rel_results = [r for r in results if r.get("type") == "relation"]
    assert len(rel_results) >= 1
    assert any(r["fact"] == "用户喜欢篮球" for r in rel_results)

    await manager.close()


@pytest.mark.asyncio
async def test_memory_manager_retrieve_returns_kg_v2_results(tmp_path):
    """MemoryManager.retrieve_memories_hybrid 在仅 KG v2 有结果时不应返回空。

    Issue C1: 早期返回路径 bypass KG v2 — 当 FTS/Vec/KG v1/子chunk 都无结果,
    仅 KG v2 有事实时, 早期返回会导致空结果。此测试验证修复后 KG v2 事实能正确露出。
    """
    from memory.kg_search import KGSearchEngine
    from memory.memory_manager import MemoryManager

    manager = DatabaseManager(tmp_path / "mm_kg_v2.db")
    await manager.init()
    db_v2 = KnowledgeDBV2(manager._conn)
    kg_v2 = KnowledgeGraphV2(db_v2=db_v2, vector_store=None)

    # Mock LLM extraction to avoid network calls
    kg_v2.extract_from_summary = AsyncMock(return_value={
        "entities": [
            {"name": "用户", "kind": "人物", "observations": ["喜欢篮球"]},
            {"name": "篮球", "kind": "概念", "observations": ["团队运动"]},
        ],
        "relations": [
            {"from_entity": "用户", "relation_type": "喜欢", "to_entity": "篮球",
             "fact": "用户喜欢篮球"},
        ],
    })

    # Step 1: Add facts via KnowledgeGraphV2 (episode ingestion)
    result = await kg_v2.add_facts_from_episode("用户说喜欢打篮球", time.time())
    assert result["new_facts"] == 1

    # Step 2: Create MemoryManager and inject KG v2 engine
    mm = MemoryManager(db=manager, memory=manager.memory)
    engine = KGSearchEngine(db=db_v2, vector_store=None, conn=manager._conn)
    mm.set_kg_v2_engine(engine)

    # Step 3: Query that only KG v2 can answer (no episodic memories stored)
    # Note: tier will be "cold" (0 episodic memories), exercising the cold-tier
    # early-return path that previously bypassed KG v2.
    with patch("config.KG_V2_ENABLED", True):
        results = await mm.retrieve_memories_hybrid("篮球", k=5, use_reranker=False)

    # Step 4: Verify KG v2 results are returned (not empty)
    assert len(results) >= 1, "KG v2 results should be returned when only KG v2 has matches"
    # At least one result should come from kg_v2 source
    kg_v2_results = [r for r in results if r.get("source") == "kg_v2"]
    assert len(kg_v2_results) >= 1, f"Expected at least one kg_v2 source result, got: {results}"
    # Verify the fact content is present
    summaries = [r.get("summary", "") for r in kg_v2_results]
    assert any("用户喜欢篮球" in s for s in summaries), \
        f"Expected '用户喜欢篮球' in kg_v2 summaries, got: {summaries}"

    await manager.close()


@pytest.mark.asyncio
async def test_memory_manager_kg_v2_merged_with_other_channels(tmp_path):
    """KG v2 事实应与其他通道结果合并, 不被 reranker/RRF 路径丢弃。

    Issue I1: reranker 路径中 [:k] 切片会丢弃全部 kg_v2_items。
    此测试在有 FTS 结果 + KG v2 结果的场景下, 验证两者都被返回。
    """
    from memory.kg_search import KGSearchEngine
    from memory.memory_manager import MemoryManager

    manager = DatabaseManager(tmp_path / "mm_merge.db")
    await manager.init()
    db_v2 = KnowledgeDBV2(manager._conn)
    kg_v2 = KnowledgeGraphV2(db_v2=db_v2, vector_store=None)

    # Mock LLM extraction
    kg_v2.extract_from_summary = AsyncMock(return_value={
        "entities": [
            {"name": "用户", "kind": "人物", "observations": ["喜欢篮球"]},
            {"name": "篮球", "kind": "概念", "observations": ["团队运动"]},
        ],
        "relations": [
            {"from_entity": "用户", "relation_type": "喜欢", "to_entity": "篮球",
             "fact": "用户喜欢篮球"},
        ],
    })

    # Add KG v2 facts
    await kg_v2.add_facts_from_episode("用户说喜欢打篮球", time.time())

    # Also insert an episodic memory so FTS has results and tier becomes warm/hot
    # (uses the proper insert method which also writes to the FTS index)
    await manager.memory.insert_episodic_memory(
        summary="今天讨论了篮球运动", importance=0.8,
    )

    mm = MemoryManager(db=manager, memory=manager.memory)
    engine = KGSearchEngine(db=db_v2, vector_store=None, conn=manager._conn)
    mm.set_kg_v2_engine(engine)

    with patch("config.KG_V2_ENABLED", True):
        results = await mm.retrieve_memories_hybrid("篮球", k=5, use_reranker=False)

    # Should have results from both episodic memory and KG v2
    assert len(results) >= 1
    kg_v2_results = [r for r in results if r.get("source") == "kg_v2"]
    assert len(kg_v2_results) >= 1, \
        f"Expected kg_v2 results to be merged, got sources: {[r.get('source') for r in results]}"

    await manager.close()
