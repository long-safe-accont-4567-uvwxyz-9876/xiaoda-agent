"""Confirm/Correct 机制单元测试"""
import json

import aiosqlite
import pytest

from db.db_concept import ConceptDB
from memory.confirm_correct import ConfirmCorrect
from memory.key_extractor import KeyExtractor
from memory.spreading_activation import SpreadingActivationEngine


@pytest.fixture
async def cc():
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
    engine = SpreadingActivationEngine(cdb, vector_store=None, key_extractor=ke)

    # Mock memory_db with async increment_access_count
    class MockMemoryDB:
        async def increment_access_count(self, mem_id):
            pass
    cc_instance = ConfirmCorrect(cdb, engine, MockMemoryDB(), ke)
    yield cc_instance
    await conn.close()


@pytest.mark.asyncio
async def test_confirm_increases_weight(cc):
    now = "2026-07-10T12:00:00+08:00"
    import time as _time
    now_ts = _time.time()
    await cc.db.insert_node(
        id="node1", text="Redis 是数据库",
        keys=json.dumps(["redis", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
        weight=0.5, peak_weight=0.5,
        difficulty=5.0, stability=3.0, phase="buffer",
        last_review=now_ts, reinforcement_count=0,
    )
    result = await cc.confirm(["node1"])
    assert result["reinforced"] == 1
    node = await cc.db.get_node("node1")
    assert node["access_count"] == 1
    # FSRS reinforce 后 R 接近 1.0，weight 由 R 驱动
    assert node["weight"] > 0.5
    assert node["peak_weight"] >= node["weight"]


@pytest.mark.asyncio
async def test_confirm_caps_at_1(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="node1", text="test", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
        weight=0.95,  # 接近上限
    )
    await cc.confirm(["node1"])
    node = await cc.db.get_node("node1")
    assert node["weight"] == 1.0  # min(1.0, 0.95+0.15)


@pytest.mark.asyncio
async def test_confirm_unknown_node(cc):
    result = await cc.confirm(["nonexistent"])
    assert result["reinforced"] == 0
    assert result["unknown"] == 1


@pytest.mark.asyncio
async def test_confirm_reinforces_edges(cc):
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2"]:
        await cc.db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await cc.db.create_edge("n1", "n2", "co-occurrence", 0.5, now)
    await cc.db.create_edge("n2", "n1", "co-occurrence", 0.5, now)

    await cc.confirm(["n1"])
    # 边权重应增加 0.25
    edges_n1 = await cc.db.get_edges("n1")
    assert edges_n1["n2"]["weight"] == 0.75  # 0.5 + 0.25
    edges_n2 = await cc.db.get_edges("n2")
    assert edges_n2["n1"]["weight"] == 0.75  # 双向同步


@pytest.mark.asyncio
async def test_correct_creates_new_node(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="oldnode", text="Python 是编译型语言",  # 错误
        keys=json.dumps(["python", "编译", "语言"]),
        created=now, last_accessed=now, valid_from=now,
        weight=0.8, peak_weight=0.9, confidence=0.5,
    )

    result = await cc.correct("Python 编译型语言", "Python 是解释型语言")
    assert "error" not in result
    assert result["old_id"] == "oldnode"
    assert result["new_id"] != "oldnode"

    # 旧节点应被关闭
    old = await cc.db.get_node("oldnode")
    assert old["valid_to"] is not None
    assert old["superseded_by"] == result["new_id"]

    # 新节点应存在且有效
    new = await cc.db.get_node(result["new_id"])
    assert new is not None
    assert new["valid_to"] is None
    assert "解释" in new["text"]
    assert new["confidence"] == 0.35  # 0.5 × 0.7


@pytest.mark.asyncio
async def test_correct_no_match(cc):
    result = await cc.correct("完全不相关的查询", "新文本")
    assert "error" in result
    assert result["error"] == "no match"


@pytest.mark.asyncio
async def test_correct_insufficient_match_quality(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="nodeX", text="完全不同的内容XYZ",
        keys=json.dumps(["完全", "不同", "内容"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # hint 只有1个 token 重叠
    result = await cc.correct("内容A", "新文本")
    assert "error" in result


@pytest.mark.asyncio
async def test_correct_migrates_edges(cc):
    now = "2026-07-10T12:00:00+08:00"
    # old → other 边
    await cc.db.insert_node(
        id="old", text="Redis 数据库缓存",
        keys=json.dumps(["redis", "数据库", "缓存"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await cc.db.insert_node(
        id="other", text="PostgreSQL 数据库",
        keys=json.dumps(["postgresql", "数据库"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await cc.db.create_edge("old", "other", "co-occurrence", 0.8, now)
    await cc.db.create_edge("other", "old", "co-occurrence", 0.8, now)

    result = await cc.correct("Redis 数据库", "Redis 缓存数据库系统")
    new_id = result["new_id"]

    # 新节点应有到 other 的边
    new_edges = await cc.db.get_edges(new_id)
    assert "other" in new_edges


@pytest.mark.asyncio
async def test_correct_supersedes_edge(cc):
    now = "2026-07-10T12:00:00+08:00"
    await cc.db.insert_node(
        id="old2", text="Redis 数据库缓存",
        keys=json.dumps(["redis", "数据库", "缓存"]),
        created=now, last_accessed=now, valid_from=now,
    )
    result = await cc.correct("Redis 数据库", "Redis 缓存数据库系统")
    new_id = result["new_id"]
    old_id = result["old_id"]

    # supersedes 边
    new_edges = await cc.db.get_edges(new_id)
    assert old_id in new_edges
    assert new_edges[old_id]["relation"] == "supersedes"

    old_edges = await cc.db.get_edges(old_id)
    assert new_id in old_edges
    assert old_edges[new_id]["relation"] == "superseded-by"


def test_constants(cc):
    assert ConfirmCorrect.EDGE_BOOST == 0.25
