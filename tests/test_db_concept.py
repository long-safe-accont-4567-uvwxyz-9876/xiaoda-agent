"""概念图数据库 CRUD 单元测试"""
import json

import aiosqlite
import pytest

from db.db_concept import ConceptDB


@pytest.fixture
async def engine():
    """内存库 + ConceptDB（与 test_spreading_activation 同构）。"""
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
    yield ConceptDB(conn)
    await conn.close()



@pytest.fixture
async def concept_db():
    """临时内存 SQLite 数据库 + 概念图表"""
    db_path = ":memory:"
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    # 创建 concept 表
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS concept_nodes (
            id            TEXT PRIMARY KEY,
            text          TEXT NOT NULL,
            weight        REAL NOT NULL DEFAULT 1.0,
            peak_weight   REAL NOT NULL DEFAULT 1.0,
            confidence    REAL NOT NULL DEFAULT 1.0,
            access_count  INTEGER NOT NULL DEFAULT 0,
            keys          TEXT NOT NULL DEFAULT '[]',
            layer         TEXT NOT NULL DEFAULT 'hippocampus',
            created       TEXT NOT NULL,
            last_accessed TEXT NOT NULL,
            valid_from    TEXT NOT NULL,
            valid_to      TEXT,
            superseded_by TEXT,
            history       TEXT NOT NULL DEFAULT '[]',
            origin        TEXT NOT NULL DEFAULT '{}',
            source_mem_id INTEGER,
            embedding     BLOB,
            difficulty    REAL NOT NULL DEFAULT 5.0,
            stability     REAL NOT NULL DEFAULT 3.0,
            phase         TEXT NOT NULL DEFAULT 'buffer',
            last_review   REAL NOT NULL DEFAULT 0.0,
            reinforcement_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS concept_edges (
            source_id  TEXT NOT NULL,
            target_id  TEXT NOT NULL,
            relation   TEXT NOT NULL DEFAULT 'related',
            weight     REAL NOT NULL DEFAULT 1.0,
            created    TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS concept_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    await conn.commit()
    cdb = ConceptDB(conn)
    yield cdb
    await conn.close()


@pytest.mark.asyncio
async def test_insert_and_get_node(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="abc123def456", text="Redis 是内存数据库",
        keys=json.dumps(["redis", "数据库", "内存"]),
        created=now, last_accessed=now, valid_from=now,
    )
    node = await concept_db.get_node("abc123def456")
    assert node is not None
    assert node["text"] == "Redis 是内存数据库"
    assert node["weight"] == 1.0
    assert node["valid_to"] is None


@pytest.mark.asyncio
async def test_get_node_not_found(concept_db):
    node = await concept_db.get_node("nonexistent")
    assert node is None


@pytest.mark.asyncio
async def test_get_node_by_source_mem(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="src123", text="test", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
        source_mem_id=42,
    )
    node = await concept_db.get_node_by_source_mem(42)
    assert node is not None
    assert node["id"] == "src123"


@pytest.mark.asyncio
async def test_update_node(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="upd123", text="test", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.update_node("upd123", weight=0.8, access_count=3,
                                 peak_weight=0.9, last_accessed=now)
    node = await concept_db.get_node("upd123")
    assert node["weight"] == 0.8
    assert node["access_count"] == 3
    assert node["peak_weight"] == 0.9


@pytest.mark.asyncio
async def test_get_alive_nodes(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="alive1", text="alive", keys='["a"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.insert_node(
        id="dead1", text="dead", keys='["b"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.update_node("dead1", valid_to=now)
    alive = await concept_db.get_alive_nodes()
    assert "alive1" in alive
    assert "dead1" not in alive


@pytest.mark.asyncio
async def test_get_node_count(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    assert await concept_db.get_node_count() == 0
    await concept_db.insert_node(
        id="cnt1", text="a", keys='["x"]',
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.insert_node(
        id="cnt2", text="b", keys='["y"]',
        created=now, last_accessed=now, valid_from=now,
    )
    assert await concept_db.get_node_count() == 2


@pytest.mark.asyncio
async def test_create_and_get_edges(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2", "n3"]:
        await concept_db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await concept_db.create_edge("n1", "n2", "co-occurrence", 1.0, now)
    await concept_db.create_edge("n1", "n3", "related", 0.5, now)
    edges = await concept_db.get_edges("n1")
    assert "n2" in edges
    assert "n3" in edges
    assert edges["n2"]["relation"] == "co-occurrence"
    assert edges["n3"]["weight"] == 0.5


@pytest.mark.asyncio
async def test_update_edge(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2"]:
        await concept_db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await concept_db.create_edge("n1", "n2", "related", 1.0, now)
    await concept_db.update_edge("n1", "n2", weight=0.7)
    edges = await concept_db.get_edges("n1")
    assert edges["n2"]["weight"] == 0.7


@pytest.mark.asyncio
async def test_get_edge_snapshot(concept_db):
    """整图快照与逐节点 get_edges 结果一致（含 weight 数值化）。"""
    now = "2026-07-10T12:00:00+08:00"
    for nid in ["n1", "n2", "n3"]:
        await concept_db.insert_node(
            id=nid, text=f"text_{nid}", keys='["k"]',
            created=now, last_accessed=now, valid_from=now,
        )
    await concept_db.create_edge("n1", "n2", "co-occurrence", 1.0, now)
    await concept_db.create_edge("n1", "n3", "related", 0.5, now)
    await concept_db.create_edge("n2", "n1", "defined", 0.8, now)

    snap = await concept_db.get_edge_snapshot()
    assert "n1" in snap and "n2" in snap
    assert snap["n1"]["n2"] == 1.0
    assert snap["n1"]["n3"] == 0.5
    assert snap["n2"]["n1"] == 0.8
    assert isinstance(snap["n1"]["n2"], float)


@pytest.mark.asyncio
async def test_auto_link_3_shared_keys(concept_db):
    now = "2026-07-10T12:00:00+08:00"
    # 节点 A 有 3 个 keys
    await concept_db.insert_node(
        id="nodeA", text="Python web 开发用 FastAPI",
        keys=json.dumps(["python", "web", "开发", "fastapi"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 B 共享 3 个 keys
    await concept_db.insert_node(
        id="nodeB", text="Python web 框架对比",
        keys=json.dumps(["python", "web", "框架", "对比"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 C 只共享 1 个 key
    await concept_db.insert_node(
        id="nodeC", text="Java 开发",
        keys=json.dumps(["java", "开发"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # nodeC 的 keys
    new_keys = ["python", "web", "开发", "新内容"]
    count = await concept_db.auto_link("nodeC", new_keys, min_shared=3)
    # nodeA 共享 python/web/开发 = 3 → 建边
    # nodeB 共享 python/web = 2 → 不建边
    assert count == 1
    edges = await concept_db.get_edges("nodeC")
    assert "nodeA" in edges
    assert edges["nodeA"]["relation"] == "co-occurrence"


@pytest.mark.asyncio
async def test_meta_get_set(concept_db):
    assert await concept_db.get_meta("nonexistent") is None
    await concept_db.set_meta("last_edge_decay", "2026-07-10T12:00:00+08:00")
    val = await concept_db.get_meta("last_edge_decay")
    assert val == "2026-07-10T12:00:00+08:00"


@pytest.mark.asyncio
async def test_batch_link_recent_below_threshold_no_edges(concept_db):
    """N10 回归测试：batch_link_recent 共享 keys 低于阈值时不建边。

    验证：当节点间共享 keys 数 < min_shared 时，不补建边（linked == 0）。
    正向用例（建边）由 test_batch_link_recent_executemany_writes_correctly 覆盖。
    """
    now = "2026-07-10T12:00:00+08:00"
    # 节点 A 已有边（不应被 curator 处理）
    await concept_db.insert_node(
        id="nodeA", text="Python web 开发",
        keys=json.dumps(["python", "web", "开发"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.create_edge("nodeA", "nodeX", "manual", 1.0, now)

    # 节点 B 无边，共享 3 keys with nodeA → 应被 curator 建边
    await concept_db.insert_node(
        id="nodeB", text="Python web 框架",
        keys=json.dumps(["python", "web", "框架"]),
        created=now, last_accessed=now, valid_from=now,
    )
    # 节点 C 无边，共享 1 key with nodeA → 不应建边（< min_shared=3）
    await concept_db.insert_node(
        id="nodeC", text="Java 开发",
        keys=json.dumps(["java", "开发"]),
        created=now, last_accessed=now, valid_from=now,
    )

    # batch_link_recent 只处理无边节点（B 和 C）
    linked = await concept_db.batch_link_recent(batch_size=10, min_shared=3)
    # nodeB 与 nodeA 共享 python/web = 2 < 3 → 不建边
    # nodeB 与 nodeC 共享 0 → 不建边
    # nodeC 与 nodeA 共享 开发 = 1 < 3 → 不建边
    # nodeC 与 nodeB 共享 0 → 不建边
    # 预期 0 条边（所有共享都 < 3）
    assert linked == 0


@pytest.mark.asyncio
async def test_batch_link_recent_max_edges_limit(concept_db):
    """N10 修复：max_edges_per_run 限制单次写入边数。

    验证：当潜在逻辑链接数超过 max_edges_per_run 时，只写入 max_edges_per_run
    对双向边。CodeRabbit #7：max_edges_per_run 计数逻辑链接（一条无向边 =
    2 次有向 INSERT），切片在完整逻辑链接边界，永不产生单向边。
    """
    now = "2026-07-10T12:00:00+08:00"
    # 创建一个共享很多 keys 的节点群
    # nodeTarget 与 5 个节点都共享 >= 3 keys → 5×2=10 条双向边
    await concept_db.insert_node(
        id="target", text="target node",
        keys=json.dumps(["k1", "k2", "k3", "k4"]),
        created=now, last_accessed=now, valid_from=now,
    )
    for i in range(5):
        await concept_db.insert_node(
            id=f"node{i}", text=f"node {i}",
            keys=json.dumps(["k1", "k2", "k3", f"unique_{i}"]),
            created=now, last_accessed=now, valid_from=now,
        )
    # target 已有边（不会被 curator 处理），node0-4 无边
    # 但 batch_link_recent 处理的是"无边节点"，所以 node0-4 是 target
    # node0-4 互相共享 k1/k2/k3，且都与 target 共享 → 去重后共 15 个逻辑链接
    # （node0: 5个, node1: 4个新, node2: 3个新, node3: 2个新, node4: 1个新）
    # max_edges_per_run=5（逻辑链接）→ 切片到 5 对 = 10 行有向边
    # CodeRabbit #2: 返回值为逻辑链接数，故 linked == 5
    linked = await concept_db.batch_link_recent(
        batch_size=10, min_shared=3, max_edges_per_run=5)
    assert linked == 5, f"max_edges_per_run=5 应写 5 个逻辑链接，实际：{linked}"

    # CodeRabbit #7 双向完整性：每条 (src,tgt) 都应有对应 (tgt,src)
    async with concept_db._conn.execute(
        "SELECT source_id, target_id FROM concept_edges WHERE relation='co-occurrence'"
    ) as cur:
        rows = await cur.fetchall()
    edge_set = {(r["source_id"], r["target_id"]) for r in rows}
    for src, tgt in list(edge_set):
        assert (tgt, src) in edge_set, (
            f"单向边泄漏：存在 ({src},{tgt}) 但无反向 ({tgt},{src})")


@pytest.mark.asyncio
async def test_batch_link_recent_executemany_writes_correctly(concept_db):
    """N10 修复：executemany 批量写入的边数据正确。

    验证 executemany 写入的边与原 create_edge 逐条写入的格式一致：
    - relation = "co-occurrence"
    - weight = 1.0
    - 双向边都存在
    """
    now = "2026-07-10T12:00:00+08:00"
    # 两个无边节点共享 3 keys
    await concept_db.insert_node(
        id="node1", text="node1",
        keys=json.dumps(["a", "b", "c"]),
        created=now, last_accessed=now, valid_from=now,
    )
    await concept_db.insert_node(
        id="node2", text="node2",
        keys=json.dumps(["a", "b", "c"]),
        created=now, last_accessed=now, valid_from=now,
    )

    linked = await concept_db.batch_link_recent(batch_size=10, min_shared=3)
    assert linked > 0, "应建边（共享 3 keys）"

    # 验证双向边都存在
    edges_1 = await concept_db.get_edges("node1")
    edges_2 = await concept_db.get_edges("node2")
    assert "node2" in edges_1, "node1→node2 边应存在"
    assert "node1" in edges_2, "node2→node1 边应存在"
    # 验证字段正确
    assert edges_1["node2"]["relation"] == "co-occurrence"
    assert edges_1["node2"]["weight"] == 1.0


# ── alive_nodes TTL 快照（2026-08-25 性能专项：USB 盘冷读 1.7s）────────


@pytest.mark.asyncio
async def test_alive_nodes_ttl_snapshot(concept_db, monkeypatch):
    """全量读取走 TTL 缓存：命中不回库、浅拷贝防污染、过期后重新回库。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr("db.db_concept.time.monotonic", lambda: clock["t"])

    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="c1", text="缓存测试节点", keys=json.dumps(["k"]),
        created=now, last_accessed=now, valid_from=now)

    first = await concept_db.get_alive_nodes()
    assert "c1" in first

    # TTL 内第二次调用：命中快照，且返回浅拷贝（改返回值不得污染缓存）
    second = await concept_db.get_alive_nodes()
    assert second is not first and second["c1"] is not first["c1"]
    second["c1"]["text"] = "污染尝试"
    third = await concept_db.get_alive_nodes()
    assert third["c1"]["text"] == "缓存测试节点"

    # TTL 过期后重新回库
    clock["t"] += 61
    fourth = await concept_db.get_alive_nodes()
    assert "c1" in fourth


@pytest.mark.asyncio
async def test_structural_write_invalidates_alive_cache(concept_db, monkeypatch):
    """结构性字段更新立即失效快照；纯统计字段更新容忍 ≤TTL 陈旧。"""
    clock = {"t": 2000.0}
    monkeypatch.setattr("db.db_concept.time.monotonic", lambda: clock["t"])

    now = "2026-07-10T12:00:00+08:00"
    await concept_db.insert_node(
        id="s1", text="旧文本", keys=json.dumps(["k"]),
        created=now, last_accessed=now, valid_from=now)
    cached = await concept_db.get_alive_nodes()
    assert cached["s1"]["text"] == "旧文本"

    # 纯统计字段更新：不失效（touch 批量更新的高频路径）
    await concept_db.update_node("s1", access_count=5)
    still = await concept_db.get_alive_nodes()
    assert still["s1"]["text"] == "旧文本"  # 快照容忍统计字段陈旧

    # 结构性字段更新（text）：立即失效并反映新值
    await concept_db.update_node("s1", text="新文本", auto_commit=False)
    await concept_db._conn.commit()
    fresh = await concept_db.get_alive_nodes()
    assert fresh["s1"]["text"] == "新文本"
