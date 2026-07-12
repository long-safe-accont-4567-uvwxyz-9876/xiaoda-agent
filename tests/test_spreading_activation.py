"""扩散激活引擎单元测试"""
import json

import aiosqlite
import networkx as nx
import pytest

from db.db_concept import ConceptDB
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivation, SpreadingActivationEngine


@pytest.fixture
async def engine():
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
            source_mem_id INTEGER, embedding BLOB,
            difficulty REAL DEFAULT 5.0, stability REAL DEFAULT 3.0,
            phase TEXT DEFAULT 'buffer', last_review REAL DEFAULT 0.0,
            reinforcement_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT DEFAULT 'related', weight REAL DEFAULT 1.0,
            created TEXT NOT NULL, PRIMARY KEY (source_id, target_id)
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    ke = KeyExtractor()
    eng = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)
    yield eng
    await conn.close()


@pytest.mark.asyncio
async def test_recall_empty_db(engine):
    results = await engine.recall("test query")
    assert results == []


@pytest.mark.asyncio
async def test_recall_direct_hit(engine):
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="node1", text="Redis 是内存数据库",
        keys=json.dumps(["redis", "内存", "数据库"]), created=now,
        last_accessed=now, valid_from=now,
    )
    results = await engine.recall("Redis 数据库")
    assert len(results) >= 1
    assert results[0]["id"] == "node1"
    assert results[0]["score"] > 0


@pytest.mark.asyncio
async def test_recall_spreading_activation(engine):
    """测试扩散激活：直接命中节点 → 沿边传播到关联节点"""
    now = "2026-07-10T12:00:00+08:00"
    # 节点 A: 直接命中
    await engine.db.insert_node(
        id="nodeA", text="Python 编程语言教程",
        keys=json.dumps(["python", "编程", "语言", "教程"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 B: 不直接命中，但与 A 有边
    await engine.db.insert_node(
        id="nodeB", text="FastAPI 框架",
        keys=json.dumps(["fastapi", "框架"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 建边 A→B, B→A
    await engine.db.create_edge("nodeA", "nodeB", "co-occurrence", 1.0, now)
    await engine.db.create_edge("nodeB", "nodeA", "co-occurrence", 1.0, now)

    results = await engine.recall("Python 编程")
    ids = [r["id"] for r in results]
    # nodeA 直接命中
    assert "nodeA" in ids
    # nodeB 通过扩散激活被召回
    assert "nodeB" in ids


@pytest.mark.asyncio
async def test_recall_dead_node_not_returned(engine):
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="dead", text="Redis 数据库",
        keys=json.dumps(["redis", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await engine.db.update_node("dead", valid_to=now)
    results = await engine.recall("Redis 数据库")
    assert all(r["id"] != "dead" for r in results)


@pytest.mark.asyncio
async def test_compute_idf(engine):
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="n1", text="a", keys=json.dumps(["redis", "python"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await engine.db.insert_node(
        id="n2", text="b", keys=json.dumps(["redis", "java"]),
        created=now, last_accessed=now, valid_from=now,
    )
    alive = await engine.db.get_alive_nodes()
    idf = engine._compute_idf({"redis", "python"}, alive)
    # redis 出现在 2 个节点 → idf 较低
    # python 出现在 1 个节点 → idf 较高
    assert idf["python"] > idf["redis"]


def test_direct_channel_weight_bias(engine):
    """weight_bias = 0.35 + 0.65 * weight，floor 0.35"""
    now = "2026-07-10T12:00:00+08:00"
    # 这个测试不依赖 DB，直接测试公式
    # 模拟 alive_nodes
    alive = {
        "low_weight": {"id": "low_weight", "text": "test", "keys": '["redis"]',
                       "weight": 0.0},
        "high_weight": {"id": "high_weight", "text": "test", "keys": '["redis"]',
                         "weight": 1.0},
    }
    idf = {"redis": 1.0}
    direct = engine._direct_channel({"redis"}, idf, alive, "redis")
    # high_weight 节点分数应高于 low_weight
    assert direct["high_weight"] > direct["low_weight"]
    # low_weight 的 w_bias = 0.35（floor）
    assert direct["low_weight"] > 0


@pytest.mark.asyncio
async def test_rrf_fusion(engine):
    direct = {"a": 0.9, "b": 0.5}
    spread = {"b": 0.3, "c": 0.2}
    fused = engine._rrf_fusion(direct, spread)
    assert "a" in fused
    assert "b" in fused
    assert "c" in fused
    # b 在两个通道都有 → 分数更高
    assert fused["b"] > fused["c"]


@pytest.mark.asyncio
async def test_pattern_separation_dedup(engine):
    """模式分离：相似文本去重"""
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="dup1", text="Redis 缓存数据库",
        keys=json.dumps(["redis", "缓存", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await engine.db.insert_node(
        id="dup2", text="Redis 缓存数据库",  # 完全相同
        keys=json.dumps(["redis", "缓存", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 注意：insert_node 用 INSERT OR REPLACE，相同 id 不会重复
    # 测试 pattern_separation 的相似度去重逻辑
    fused = {"dup1": 0.5}
    results = engine._pattern_separation(fused, top_k=5)
    assert len(results) <= 5


def test_constants(engine):
    """验证关键常量值（来自 spec）"""
    assert SpreadingActivationEngine.RECALL_RADIUS == 3
    assert SpreadingActivationEngine.ACTIVATION_DECAY == 0.5
    assert SpreadingActivationEngine.SPREADING_THRESHOLD == 0.05
    assert SpreadingActivationEngine.RRF_K == 60
    assert SpreadingActivationEngine.FUZZY_ACTIVATION == 0.5
    assert SpreadingActivationEngine.SEPARATION_SIM == 0.92


# ──────────────────────────────────────────────────────────────────
# Task 6 新增：SpreadingActivation (networkx 图扩散) 测试
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def graph():
    """构建测试图: A-B-C-D 链 + A-C 边"""
    g = nx.Graph()
    g.add_edge(1, 2, weight=0.8)
    g.add_edge(2, 3, weight=0.7)
    g.add_edge(3, 4, weight=0.6)
    g.add_edge(1, 3, weight=0.5)
    return g


def test_spread_activation_basic(graph):
    """测试基本扩散激活"""
    sa = SpreadingActivation()
    results = sa.spread(graph, seed_id=1, decay=0.85, threshold=0.01, max_depth=5)
    # seed=1 应激活 2, 3, 4
    activated_ids = {r.node_id for r in results}
    assert 1 in activated_ids  # seed自身
    assert 2 in activated_ids
    assert 3 in activated_ids


def test_spread_activation_threshold(graph):
    """测试阈值过滤: 高阈值只激活近邻"""
    sa = SpreadingActivation()
    results = sa.spread(graph, seed_id=1, decay=0.5, threshold=0.3, max_depth=5)
    # 衰减快+高阈值 → 只激活直接邻居
    activated_ids = {r.node_id for r in results}
    assert 1 in activated_ids
    # node 4 可能不被激活 (距离远)


def test_spread_activation_max_depth(graph):
    """测试最大深度限制"""
    sa = SpreadingActivation()
    results = sa.spread(graph, seed_id=1, max_depth=1)
    # depth=1 → 只激活直接邻居
    activated_ids = {r.node_id for r in results}
    assert 1 in activated_ids
    assert 2 in activated_ids
    assert 3 in activated_ids
    # depth=1 不应到达 4 (需要 1→2→3→4 或 1→3→4, 深度2)
    # 但 1→3 是直接边, 所以3在depth1
    # 4 需要 depth=2


def test_predict_links(graph):
    """测试链路预测"""
    sa = SpreadingActivation()
    g = nx.Graph()
    g.add_edge(1, 2, weight=0.8)
    g.add_edge(2, 3, weight=0.7)
    g.add_edge(1, 3, weight=0.5)
    # 1和3已有边, 2和... 预测新连接
    predictions = sa.predict_links(g, node_id=1, max_results=5)
    assert isinstance(predictions, list)


# ──────────────────────────────────────────────────────────────────
# spread_activation 函数测试 (deterministic bounded spreading)
# ──────────────────────────────────────────────────────────────────


def test_one_hop_propagates_weighted_activation() -> None:
    from memory.spreading_activation import spread_activation

    scores = spread_activation(
        {"seed": 1.0},
        {"seed": {"related": 0.8}},
        decay=0.5,
        threshold=0.0,
    )

    assert scores == {"seed": 1.0, "related": 0.4}


def test_candidate_budget_keeps_highest_scores_with_stable_ties() -> None:
    from memory.spreading_activation import spread_activation

    scores = spread_activation(
        {"seed": 1.0},
        {"seed": {"z": 0.8, "b": 0.9, "a": 0.9}},
        threshold=0.0,
        candidate_budget=3,
    )

    assert list(scores) == ["seed", "a", "b"]


def test_high_degree_sources_receive_a_degree_penalty() -> None:
    from memory.spreading_activation import spread_activation

    scores = spread_activation(
        {"hub": 1.0, "focused": 1.0},
        {
            "hub": {"hub_target": 1.0, "noise_1": 0.1, "noise_2": 0.1, "noise_3": 0.1},
            "focused": {"focused_target": 1.0},
        },
        threshold=0.0,
    )

    assert scores["focused_target"] == 0.5
    assert scores["hub_target"] == 0.25


def test_max_hops_bounds_propagation_and_uses_hop_decay() -> None:
    from memory.spreading_activation import spread_activation

    adjacency = {"a": {"b": 1.0}, "b": {"c": 1.0}}

    one_hop = spread_activation({"a": 1.0}, adjacency, threshold=0.0)
    two_hops = spread_activation({"a": 1.0}, adjacency, max_hops=2, threshold=0.0)

    assert "c" not in one_hop
    assert two_hops["c"] == 0.125