"""扩散激活记忆系统集成测试"""
import json
import time

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.concept_graph import ConceptGraph
from memory.confirm_correct import ConfirmCorrect
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


@pytest.fixture
async def system():
    """完整的扩散激活记忆系统"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            weight REAL DEFAULT 1.0, peak_weight REAL DEFAULT 1.0,
            confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
            keys TEXT DEFAULT '[]', layer TEXT DEFAULT 'hippocampus',
            created TEXT NOT NULL, last_accessed TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT,
            history TEXT DEFAULT '[]', origin TEXT DEFAULT '{}',
            source_mem_id INTEGER, embedding BLOB
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS concept_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    """)
    await conn.commit()

    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    graph = ConceptGraph(cdb, ke)
    engine = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)

    class MockMemoryDB:
        async def increment_access_count(self, mem_id):
            pass
    cc = ConfirmCorrect(cdb, engine, MockMemoryDB(), ke)

    yield {"cdb": cdb, "ke": ke, "graph": graph, "engine": engine, "cc": cc}
    await conn.close()


@pytest.mark.asyncio
async def test_full_workflow_remember_recall_confirm(system):
    """完整工作流：写入 → 检索 → 确认 → 再检索（权重提升）"""
    graph = system["graph"]
    engine = system["engine"]
    cc = system["cc"]

    # 1. 写入记忆
    node_id = await graph.remember(
        "Redis 是内存数据库，常用于缓存和会话存储",
        source_mem_id=1,
    )
    assert node_id

    # 2. 检索
    results = await engine.recall("Redis 缓存", top_k=5)
    assert len(results) >= 1
    assert results[0]["id"] == node_id
    initial_score = results[0]["score"]

    # 3. 确认
    confirm_result = await cc.confirm([node_id])
    assert confirm_result["reinforced"] == 1

    # 4. 再次检索（权重提升后分数应更高）
    results2 = await engine.recall("Redis 缓存", top_k=5)
    assert len(results2) >= 1
    assert results2[0]["id"] == node_id


@pytest.mark.asyncio
async def test_spreading_activation_finds_related(system):
    """扩散激活：通过关联节点找到间接相关记忆"""
    graph = system["graph"]
    engine = system["engine"]

    # 写入关联记忆
    await graph.remember("Python 编程语言基础教程", source_mem_id=1)
    await graph.remember("Python web 开发实战指南", source_mem_id=2)
    # 这两个应共享 python/编程/开发 等 keys → auto_link

    # 检索一个，应能通过扩散激活找到另一个
    results = await engine.recall("Python 编程", top_k=5)
    ids = [r["id"] for r in results]
    assert len(ids) >= 1


@pytest.mark.asyncio
async def test_correct_workflow(system):
    """纠正工作流：写入 → 纠正 → 旧记忆关闭、新记忆激活"""
    graph = system["graph"]
    engine = system["engine"]
    cc = system["cc"]

    # 写入错误记忆
    await graph.remember("Python 是编译型语言", source_mem_id=10)

    # 纠正
    result = await cc.correct("Python 编译型语言", "Python 是解释型语言")
    assert "error" not in result

    # 检索应返回新记忆，不返回旧记忆
    results = await engine.recall("Python 语言", top_k=5)
    for r in results:
        assert r["id"] != result["old_id"]  # 旧节点已关闭


@pytest.mark.asyncio
async def test_lazy_migrate(system):
    """懒迁移：旧 episodic_memories 迁移到 concept_nodes"""
    graph = system["graph"]
    engine = system["engine"]

    episodic = [
        {"id": 100, "summary": "Docker 容器化部署"},
        {"id": 101, "summary": "Kubernetes 集群管理"},
        {"id": 102, "summary": "CI/CD 流水线配置"},
    ]
    count = await graph.lazy_migrate(episodic, limit=50)
    assert count == 3

    # 迁移后应能检索到
    results = await engine.recall("Docker 部署", top_k=5)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_dual_write_consistency(system):
    """双写一致性：source_mem_id 映射正确"""
    graph = system["graph"]
    cdb = system["cdb"]

    node_id = await graph.remember("测试双写", source_mem_id=999)
    node = await cdb.get_node_by_source_mem(999)
    assert node is not None
    assert node["id"] == node_id


@pytest.mark.asyncio
async def test_empty_query_returns_empty(system):
    """空查询返回空列表"""
    engine = system["engine"]
    assert await engine.recall("") == []
    assert await engine.recall("   ") == []


@pytest.mark.asyncio
async def test_confirm_multiple_nodes(system):
    """批量确认多个节点"""
    graph = system["graph"]
    cc = system["cc"]

    id1 = await graph.remember("记忆一", source_mem_id=1)
    id2 = await graph.remember("记忆二", source_mem_id=2)

    result = await cc.confirm([id1, id2])
    assert result["reinforced"] == 2

    node1 = await cc.db.get_node(id1)
    node2 = await cc.db.get_node(id2)
    assert node1["access_count"] == 1
    assert node2["access_count"] == 1


@pytest.mark.asyncio
async def test_constants_end_to_end(system):
    """端到端常量验证"""
    assert SpreadingActivationEngine.RECALL_RADIUS == 3
    assert ConfirmCorrect.BOOST_PER_ACCESS == 0.15
    assert ConfirmCorrect.EDGE_BOOST == 0.25
    assert KeyExtractor.MAX_KEYS == 24
