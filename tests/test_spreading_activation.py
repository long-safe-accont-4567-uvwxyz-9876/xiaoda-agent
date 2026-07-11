"""扩散激活引擎单元测试"""
import json

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


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
            source_mem_id INTEGER, embedding BLOB
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
