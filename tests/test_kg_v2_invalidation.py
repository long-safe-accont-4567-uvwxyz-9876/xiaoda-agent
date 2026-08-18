"""KG v2 事实超驰 + 实体演化测试。"""
import time
from unittest.mock import AsyncMock, patch

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph_v2 import KnowledgeGraphV2


@pytest.mark.asyncio
async def test_resolve_contradiction_marks_old_as_invalid(tmp_path):
    """旧事实生效更早 → 标记失效。"""
    manager = DatabaseManager(tmp_path / "sup.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    old_relation = {"valid_at": 1000.0, "invalid_at": None, "is_current": 1}
    result = kg._resolve_contradiction(old_relation, new_valid_at=2000.0)
    assert result is True
    assert old_relation["invalid_at"] == 2000.0
    assert old_relation["is_current"] == 0
    await manager.close()


@pytest.mark.asyncio
async def test_resolve_contradiction_skips_already_invalid(tmp_path):
    """旧事实已失效 → 不冲突。"""
    manager = DatabaseManager(tmp_path / "sup2.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    old_relation = {"valid_at": 1000.0, "invalid_at": 1500.0, "is_current": 0}
    result = kg._resolve_contradiction(old_relation, new_valid_at=2000.0)
    assert result is False
    await manager.close()


@pytest.mark.asyncio
async def test_detect_contradictions_via_llm(tmp_path):
    """LLM 矛盾检测返回被矛盾的索引列表。"""
    manager = DatabaseManager(tmp_path / "detect.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM to return contradiction at index 0
    kg._call_free_model = AsyncMock(return_value='{"contradicted_indices": [0]}')
    result = await kg._detect_contradictions(
        new_fact="用户改打网球了",
        existing_facts=["用户喜欢打篮球"],
    )
    assert result == [0]
    await manager.close()


@pytest.mark.asyncio
async def test_merge_relation_v2_supersedes_old_fact(tmp_path):
    """新事实超驰旧事实：旧关系 is_current→0，新关系插入。"""
    manager = DatabaseManager(tmp_path / "merge.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM contradiction detection
    kg._call_free_model = AsyncMock(return_value='{"contradicted_indices": [0]}')
    kg.extract_from_summary = AsyncMock(return_value={
        "entities": [{"name": "用户", "kind": "人物", "observations": []}],
        "relations": [],
    })

    # Insert old fact
    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_episode("EP-old", "旧对话", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-old", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-old", 1000.0)

    # Merge new contradictory fact
    new_rel = {
        "from_entity": "用户",
        "relation_type": "喜欢",
        "to_entity": "网球",
        "fact": "用户改打网球了",
    }
    is_new, invalidated = await kg.merge_relation_v2(new_rel, "EP-new", 2000.0)
    assert is_new is True
    assert len(invalidated) == 1
    assert invalidated[0]["id"] == "REL-old"

    # Verify old relation is invalidated
    active = await db.get_active_relations_between("用户", "篮球")
    assert len(active) == 0
    await manager.close()


@pytest.mark.asyncio
async def test_merge_relation_v2_deduplicates_identical_fact(tmp_path):
    """相同事实不重复插入，仅追加 episode 引用。"""
    manager = DatabaseManager(tmp_path / "dedup.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    await db.insert_entity_v2("ENT-u", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_episode("EP-1", "对话1", "summary", 1000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)

    rel = {
        "from_entity": "用户",
        "relation_type": "喜欢",
        "to_entity": "篮球",
        "fact": "用户喜欢篮球",
    }
    is_new, invalidated = await kg.merge_relation_v2(rel, "EP-2", 2000.0)
    assert is_new is False
    assert len(invalidated) == 0

    # Verify episode ref was appended
    episodes = await db.get_episodes_for_fact("REL-1")
    ep_ids = {e["id"] for e in episodes}
    assert ep_ids == {"EP-1", "EP-2"}
    await manager.close()


@pytest.mark.asyncio
async def test_merge_entities_v2_increments_summary_version(tmp_path):
    """实体演化：summary 重写，version 递增。"""
    manager = DatabaseManager(tmp_path / "evol.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM summary rewrite
    kg._call_free_model = AsyncMock(return_value="用户喜欢篮球，是篮球爱好者")

    # First insert
    await db.insert_entity_v2("ENT-u", "用户", "人物", ["喜欢篮球"], "喜欢篮球")
    # Merge with new observations
    await kg.merge_entities_v2(
        [{"name": "用户", "kind": "人物", "observations": ["每周打篮球"]}],
        episode_content="用户每周都打篮球",
        episode_time=2000.0,
    )
    ent = await db.get_entity_v2("用户")
    assert ent["summary"] == "用户喜欢篮球，是篮球爱好者"
    assert ent["summary_version"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_add_facts_from_episode_end_to_end(tmp_path):
    """完整 episode 摄入流程。"""
    manager = DatabaseManager(tmp_path / "e2e.db")
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

    result = await kg.add_facts_from_episode("用户说喜欢打篮球", 1000.0)
    assert result["new_facts"] == 1
    assert result["invalidated"] == 0
    assert result["episode_id"].startswith("EP-")

    # Verify entity and relation were created
    ent = await db.get_entity_v2("用户")
    assert ent is not None
    rel = await db.get_active_relations_between("用户", "篮球")
    assert len(rel) == 1
    await manager.close()
