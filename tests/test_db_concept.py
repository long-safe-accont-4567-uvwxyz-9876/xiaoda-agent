"""概念图数据库 CRUD 单元测试"""
import asyncio
import json
import os
import tempfile
import time

import aiosqlite
import pytest

from db.db_concept import ConceptDB


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
            embedding     BLOB
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
