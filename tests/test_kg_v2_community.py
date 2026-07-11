"""KG v2 社区发现测试 — 标签传播 + 社区摘要。"""
import time
from unittest.mock import AsyncMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.knowledge_graph_v2 import KnowledgeGraphV2


def test_label_propagation_clusters_connected_nodes():
    """标签传播: 相连实体聚类到同一社区。"""
    kg = KnowledgeGraphV2.__new__(KnowledgeGraphV2)
    adjacency = {
        "A": [("B", 2), ("C", 1)],
        "B": [("A", 2), ("C", 1)],
        "C": [("A", 1), ("B", 1)],
        "D": [("E", 2)],
        "E": [("D", 2)],
    }
    clusters = kg._label_propagation(adjacency, max_iter=10)
    assert len(clusters) >= 2
    # A, B, C should be in the same cluster
    for cluster in clusters:
        if "A" in cluster:
            assert "B" in cluster
            assert "C" in cluster
        if "D" in cluster:
            assert "E" in cluster


def test_label_propagation_empty_adjacency():
    kg = KnowledgeGraphV2.__new__(KnowledgeGraphV2)
    assert kg._label_propagation({}, max_iter=10) == []


def test_label_propagation_single_node():
    kg = KnowledgeGraphV2.__new__(KnowledgeGraphV2)
    adjacency = {"X": []}
    clusters = kg._label_propagation(adjacency, max_iter=10)
    assert len(clusters) == 1
    assert clusters[0] == ["X"]


@pytest.mark.asyncio
async def test_detect_communities_creates_community_records(tmp_path):
    """社区发现: 检测社区并写入 kg_communities 表。"""
    manager = DatabaseManager(tmp_path / "comm.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)
    # Mock LLM for community naming
    kg._call_free_model = AsyncMock(return_value="运动社区")

    # Build a graph: A-B-C cluster, D-E cluster
    await db.insert_entity_v2("ENT-a", "A", "概念", [], "A的摘要")
    await db.insert_entity_v2("ENT-b", "B", "概念", [], "B的摘要")
    await db.insert_entity_v2("ENT-c", "C", "概念", [], "C的摘要")
    await db.insert_entity_v2("ENT-d", "D", "概念", [], "D的摘要")
    await db.insert_entity_v2("ENT-e", "E", "概念", [], "E的摘要")
    await db.insert_episode("EP-1", "", "summary", 1000.0, time.time())
    await db.insert_relation_v2("R1", "A", "connected", "B", "A连接B", "EP-1", 1000.0)
    await db.insert_relation_v2("R2", "B", "connected", "C", "B连接C", "EP-1", 1000.0)
    await db.insert_relation_v2("R3", "D", "connected", "E", "D连接E", "EP-1", 1000.0)

    clusters = await kg.detect_communities()
    assert len(clusters) >= 2

    # Verify communities were created in DB
    cursor = await manager._conn.execute("SELECT COUNT(*) as cnt FROM kg_communities")
    row = await cursor.fetchone()
    assert row["cnt"] >= 1
    await manager.close()


@pytest.mark.asyncio
async def test_update_community_for_entity(tmp_path):
    """增量更新: 新实体加入邻居社区。"""
    manager = DatabaseManager(tmp_path / "incr.db")
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    kg = KnowledgeGraphV2(db_v2=db, vector_store=None)

    # Create existing community with members
    await db.insert_entity_v2("ENT-a", "A", "概念", [], "")
    await db.insert_entity_v2("ENT-b", "B", "概念", [], "")
    await db.insert_community("COM-1", "测试社区", "摘要", ["A", "B"])

    # Add new entity connected to A
    await db.insert_entity_v2("ENT-c", "C", "概念", [], "")
    await db.insert_episode("EP-1", "", "summary", 1000.0, time.time())
    await db.insert_relation_v2("R1", "C", "connected", "A", "C连接A", "EP-1", 1000.0)

    await kg.update_community_for_entity("C")
    comm_id = await db.get_entity_community("C")
    assert comm_id == "COM-1"
    await manager.close()
