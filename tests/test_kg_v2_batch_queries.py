"""KG v2 批量查询接口测试 — G12 性能优化（消除 N+1 残留）。

覆盖：
- KnowledgeDBV2.get_entities_v2_batch / get_entity_communities_batch 基础行为
- KnowledgeGraphV2.merge_entities_v2 / update_community_for_entity 调用 batch 接口
  而非单条 N+1 路径
"""
from unittest.mock import AsyncMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph_v2 import KnowledgeGraphV2

# ── get_entities_v2_batch ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_entities_v2_batch_returns_dict(tmp_path):
    """批量查询实体返回 {name: entity_dict}，未命中的 name 不出现。"""
    manager = DatabaseManager(tmp_path / "batch_ent.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)

    await db.insert_entity_v2("ENT-1", "篮球", "概念", ["团队运动"], "团队运动")
    await db.insert_entity_v2("ENT-2", "足球", "概念", ["草地运动"], "草地运动")

    result = await db.get_entities_v2_batch(["篮球", "足球", "不存在"])

    assert isinstance(result, dict)
    assert "篮球" in result
    assert "足球" in result
    assert "不存在" not in result  # 未命中不出现
    assert result["篮球"]["kind"] == "概念"
    assert result["篮球"]["summary"] == "团队运动"
    assert result["足球"]["kind"] == "概念"
    await manager.close()


@pytest.mark.asyncio
async def test_get_entities_v2_batch_empty_input(tmp_path):
    """空列表返回 {}，不发 SQL。"""
    manager = DatabaseManager(tmp_path / "batch_empty.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)

    result = await db.get_entities_v2_batch([])
    assert result == {}
    await manager.close()


# ── get_entity_communities_batch ───────────────────────────────────


@pytest.mark.asyncio
async def test_get_entity_communities_batch_returns_dict(tmp_path):
    """批量查询实体所属社区：命中返回 community_id，未命中返回 None。"""
    manager = DatabaseManager(tmp_path / "batch_comm.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)

    await db.insert_entity_v2("ENT-a", "用户", "人物", [], "")
    await db.insert_entity_v2("ENT-b", "篮球", "概念", [], "")
    await db.insert_entity_v2("ENT-c", "足球", "概念", [], "")
    await db.insert_community("COM-1", "运动社区", "摘要", ["用户", "篮球"])
    # "足球" 不归属任何社区

    result = await db.get_entity_communities_batch(["用户", "篮球", "足球", "不存在"])

    assert isinstance(result, dict)
    # 输入列表中每个 name 都应出现
    assert set(result.keys()) == {"用户", "篮球", "足球", "不存在"}
    assert result["用户"] == "COM-1"
    assert result["篮球"] == "COM-1"
    assert result["足球"] is None  # 实体存在但无社区
    assert result["不存在"] is None  # 实体不存在
    await manager.close()


@pytest.mark.asyncio
async def test_get_entity_communities_batch_empty_input(tmp_path):
    """空列表返回 {}。"""
    manager = DatabaseManager(tmp_path / "batch_comm_empty.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)

    result = await db.get_entity_communities_batch([])
    assert result == {}
    await manager.close()


# ── merge_entities_v2 使用 batch 接口 ─────────────────────────────


@pytest.mark.asyncio
async def test_merge_entities_v2_uses_batch(tmp_path):
    """merge_entities_v2 应调用 get_entities_v2_batch，而非逐条 get_entity_v2。"""
    manager = DatabaseManager(tmp_path / "merge_batch.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM 重写摘要
    kg._rewrite_summary = AsyncMock(return_value="融合摘要")

    entities = [
        {"name": "篮球", "kind": "概念", "observations": ["团队运动"]},
        {"name": "足球", "kind": "概念", "observations": ["草地运动"]},
    ]

    # 替换 batch 方法为 spy，单条方法替换为会失败的 mock
    db.get_entities_v2_batch = AsyncMock(return_value={})
    db.get_entity_v2 = AsyncMock(side_effect=AssertionError(
        "merge_entities_v2 should not call get_entity_v2 (N+1 path)"
    ))
    db.insert_entity_v2 = AsyncMock(return_value=1)

    await kg.merge_entities_v2(entities, "对话摘要", 1000.0)

    # P2-6: 验证 batch 接口被调用且参数正确
    db.get_entities_v2_batch.assert_called_once()
    # 应传入完整的实体名集合 {"篮球", "足球"}
    batch_call_args = db.get_entities_v2_batch.call_args
    expected_names = {"篮球", "足球"}
    if batch_call_args.args:
        actual_names = set(batch_call_args.args[0])
    else:
        actual_names = set(batch_call_args.kwargs.get("names", []))
    assert actual_names == expected_names, (
        f"batch 应接收实体名集合 {expected_names}, 实际: {actual_names}"
    )
    # 单条接口不应被调用
    db.get_entity_v2.assert_not_called()
    await manager.close()


# ── update_community_for_entity 使用 batch 接口 ────────────────────


@pytest.mark.asyncio
async def test_update_community_for_entity_uses_batch(tmp_path):
    """update_community_for_entity 应调用 get_entity_communities_batch。"""
    manager = DatabaseManager(tmp_path / "uc_batch.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    # 准备图：C 连接 A，A 已归属 COM-1
    await db.insert_entity_v2("ENT-a", "A", "概念", [], "")
    await db.insert_entity_v2("ENT-c", "C", "概念", [], "")
    await db.insert_community("COM-1", "测试社区", "摘要", ["A"])
    await db.insert_episode("EP-1", "", "summary", 1000.0, 1000.0)
    await db.insert_relation_v2("R1", "C", "connected", "A", "C-A", "EP-1", 1000.0)

    # 替换 batch 为 spy，单条方法替换为会失败的 mock
    db.get_entity_communities_batch = AsyncMock(return_value={"A": "COM-1"})
    db.get_entity_community = AsyncMock(side_effect=AssertionError(
        "update_community_for_entity should not call get_entity_community (N+1 path)"
    ))
    db.add_entity_to_community = AsyncMock()

    await kg.update_community_for_entity("C")

    db.get_entity_communities_batch.assert_called_once()
    db.get_entity_community.assert_not_called()
    # 应将 C 加入邻居的众数社区
    db.add_entity_to_community.assert_called_once_with("C", "COM-1")
    await manager.close()
