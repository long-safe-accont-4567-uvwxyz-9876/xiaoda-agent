"""KnowledgeDBV2 CRUD 单元测试。"""
import json
import time

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2


@pytest.mark.asyncio
async def test_episode_crud(tmp_path):
    manager = DatabaseManager(tmp_path / "crud.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    now = time.time()
    await db.insert_episode("EP-test1", "用户讨论了篮球", "summary", 1000.0, now)
    ep = await db.get_episode("EP-test1")
    assert ep is not None
    assert ep["content"] == "用户讨论了篮球"
    assert ep["source_type"] == "summary"
    assert ep["valid_at"] == 1000.0
    assert await db.get_episode("EP-nonexistent") is None
    await manager.close()


@pytest.mark.asyncio
async def test_entity_v2_crud(tmp_path):
    manager = DatabaseManager(tmp_path / "entity.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    rowid = await db.insert_entity_v2("ENT-1", "篮球", "概念", ["团队运动"], "团队运动")
    assert rowid > 0
    ent = await db.get_entity_v2("篮球")
    assert ent is not None
    assert ent["kind"] == "概念"
    assert ent["summary"] == "团队运动"
    assert ent["summary_version"] == 0

    rowid2 = await db.update_entity_summary_v2("篮球", "用户喜欢的团队运动", 1)
    assert rowid2 == rowid
    ent2 = await db.get_entity_v2("篮球")
    assert ent2["summary"] == "用户喜欢的团队运动"
    assert ent2["summary_version"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_relation_v2_crud_and_invalidation(tmp_path):
    manager = DatabaseManager(tmp_path / "rel.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-a", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")

    rowid = await db.insert_relation_v2(
        "REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0
    )
    assert rowid > 0

    active = await db.get_active_relations_between("用户", "篮球")
    assert len(active) == 1
    assert active[0]["fact"] == "用户喜欢篮球"
    assert active[0]["is_current"] == 1

    await db.invalidate_relation("REL-1", invalid_at=2000.0, expired_at=2001.0)
    active2 = await db.get_active_relations_between("用户", "篮球")
    assert len(active2) == 0
    await manager.close()


@pytest.mark.asyncio
async def test_append_episode_ref_and_bidirectional_query(tmp_path):
    manager = DatabaseManager(tmp_path / "eer.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_episode("EP-1", "内容1", "summary", 1000.0, time.time())
    await db.insert_episode("EP-2", "内容2", "summary", 2000.0, time.time())
    await db.insert_relation_v2("REL-1", "用户", "喜欢", "篮球", "用户喜欢篮球", "EP-1", 1000.0)
    await db.append_episode_ref("REL-1", "EP-2")

    facts = await db.get_facts_from_episode("EP-1")
    assert len(facts) == 1
    assert facts[0]["id"] == "REL-1"

    facts2 = await db.get_facts_from_episode("EP-2")
    assert len(facts2) == 1
    assert facts2[0]["id"] == "REL-1"

    episodes = await db.get_episodes_for_fact("REL-1")
    assert len(episodes) == 2
    ep_ids = {e["id"] for e in episodes}
    assert ep_ids == {"EP-1", "EP-2"}
    await manager.close()


@pytest.mark.asyncio
async def test_community_crud(tmp_path):
    manager = DatabaseManager(tmp_path / "comm.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    await db.insert_entity_v2("ENT-a", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_community("COM-1", "运动社区", "关于运动的社区", ["用户", "篮球"])

    comm_id = await db.get_entity_community("篮球")
    assert comm_id == "COM-1"

    await db.insert_entity_v2("ENT-c", "足球", "概念", [], "")
    await db.add_entity_to_community("足球", "COM-1")
    comm_id2 = await db.get_entity_community("足球")
    assert comm_id2 == "COM-1"
    await manager.close()
