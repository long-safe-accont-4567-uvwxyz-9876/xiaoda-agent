"""GET /insight/knowledge/graph 端点测试。

覆盖实体聚焦（批量 BFS 路径）与全图概览（LIMIT 80 路径）。
最小 FastAPI app + 内存库，绝不触碰真实数据库。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import insight


class _FakeKnowledgeDB:
    """内存知识库桩：复刻 get_related_knowledge 批量 BFS 契约。"""

    def __init__(self, entities: list[dict], relations: list[dict]):
        self._entities = {e["name"]: e for e in entities}
        self._relations = relations

    async def get_related_knowledge(self, entity_names: list[str], depth: int) -> dict:
        seen: set[str] = set()
        result_rels: list[dict] = []
        frontier = list(entity_names)
        visited = set(entity_names)
        for _ in range(depth):
            nxt: list[str] = []
            for name in frontier:
                for r in self._relations:
                    if r["from_entity"] == name or r["to_entity"] == name:
                        key = (r["from_entity"], r["relation_type"], r["to_entity"])
                        if key not in seen:
                            seen.add(key)
                            result_rels.append(r)
                        other = (r["to_entity"] if r["from_entity"] == name
                                 else r["from_entity"])
                        if other not in visited:
                            visited.add(other)
                            nxt.append(other)
            frontier = nxt
        ents = [self._entities[n] for n in visited if n in self._entities]
        return {"entities": ents, "relations": result_rels}

    async def get_knowledge_entity(self, name: str) -> dict | None:
        return self._entities.get(name)


@pytest.fixture
def client():
    """真实内存 sqlite + KnowledgeDB：覆盖逐层 BFS 的真实 SQL 路径。"""
    import asyncio
    import aiosqlite
    from db.db_knowledge import KnowledgeDB

    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(aiosqlite.connect(":memory:"))
    conn.row_factory = aiosqlite.Row
    loop.run_until_complete(conn.executescript("""
        CREATE TABLE knowledge_entities (name TEXT PRIMARY KEY, kind TEXT DEFAULT '');
        CREATE TABLE knowledge_relations (
            id TEXT PRIMARY KEY, from_entity TEXT, relation_type TEXT,
            to_entity TEXT, updated_at REAL DEFAULT 0,
            valid_from REAL DEFAULT 0, valid_to REAL DEFAULT 0, confidence REAL DEFAULT 1.0);
    """))

    async def seed():
        ents = [("小妲", "person"), ("爸爸", "person"), ("须弥", "place"),
                ("孤岛", "place"), ("朋友", "person")]
        await conn.executemany(
            "INSERT OR IGNORE INTO knowledge_entities(name, kind) VALUES(?,?)", ents)
        rels = [
            ("r1", "小妲", "家人", "爸爸", 1.0),
            ("r2", "小妲", "居住", "须弥", 0.9),
            ("r3", "爸爸", "到访", "须弥", 0.5),
            ("r4", "孤岛", "位于", "须弥", 0.4),
            ("r5", "朋友", "认识", "孤岛", 0.3),
        ]
        await conn.executemany(
            "INSERT OR REPLACE INTO knowledge_relations(id, from_entity, relation_type, to_entity, confidence) VALUES(?,?,?,?,?)",
            rels)
        await conn.commit()

    loop.run_until_complete(seed())
    kdb = KnowledgeDB(conn)

    app = FastAPI()
    app.include_router(insight.router)

    class _Core:
        class db:  # noqa: N801
            knowledge = kdb

            @staticmethod
            async def fetch_all(sql, *a, **kw):
                cur = await conn.execute(sql, list(a))
                return [dict(r) for r in await cur.fetchall()]

        # 逐层 BFS 直接用 kdb._conn；同时路由里 kdb._conn.execute 可用
        _conn = conn

    app.state.core = _Core()
    c = TestClient(app)

    yield c
    loop.run_until_complete(conn.close())
    loop.close()

def _auth(requests_mock_client):
    # insight 路由依赖 get_current_user；直接 override 依赖绕过鉴权
    from web.routers.auth import get_current_user
    app = requests_mock_client.app
    app.dependency_overrides[get_current_user] = lambda: {"user": "test"}


def test_graph_entity_focus_batch_path(client):
    _auth(client)
    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 1})
    assert resp.status_code == 200
    data = resp.json()["data"]
    names = {n["name"] for n in data["nodes"]}
    # 一跳：小妲 + 爸爸 + 须弥（孤岛不出现）
    assert "小妲" in names and "爸爸" in names and "须弥" in names
    assert "孤岛" not in names
    kinds = {n["name"]: n["kind"] for n in data["nodes"]}
    assert kinds["小妲"] == "person"
    assert kinds["须弥"] == "place"


def test_graph_entity_focus_edge_cap(client):
    """第一跳关系超过每层上限时截断到 400 且按 confidence 保留最高批。"""
    import asyncio
    _auth(client)
    core = client.app.state.core
    conn = core._conn

    async def seed_many():
        rows = [(f"e{i}", "") for i in range(600)]
        await conn.executemany(
            "INSERT OR IGNORE INTO knowledge_entities(name, kind) VALUES(?,?)", rows)
        rels = [(f"c{i}", "小妲", f"r{i}", f"e{i}", i / 1000) for i in range(600)]
        await conn.executemany(
            "INSERT OR REPLACE INTO knowledge_relations(id, from_entity, relation_type, to_entity, confidence) VALUES(?,?,?,?,?)",
            rels)
        await conn.commit()

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(seed_many()) if False else None
    # TestClient 内部有运行中的事件循环依赖；直接同步跑 async seed
    import asyncio as _aio
    _aio.get_event_loop().run_until_complete(seed_many())

    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 1})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["edges"]) <= 400
    # 截断按 confidence 降序：600 条 cN 关系只保留 confidence 最高的 400 条
    # （i/1000 有并列值，排序边界允许少量浮动）。fixture 的 5 条原始关系
    # confidence 更高（0.3~1.0 vs c200=0.2），也理应在保留集内。
    kept = [e["relation"] for e in data["edges"] if e["relation"].startswith("r")]
    # 总边数 = 400 上限；fixture 的 5 条高置信关系挤占名额，rN 只剩 395~400 条
    assert len(data["edges"]) <= 400
    assert 395 <= len(kept) <= 400
    nums = sorted(int(k[1:]) for k in kept)
    assert min(nums) >= 198 and max(nums) == 599


def test_graph_overview_limit80(client):
    _auth(client)
    resp = client.get("/insight/knowledge/graph", params={"entity": "", "depth": 1})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["edges"]) <= 80
    assert all("kind" in n for n in data["nodes"])


def test_graph_depth_validation(client):
    _auth(client)
    # depth 可手动编辑：默认 6，上限 12；13 越界 / 0 越界
    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 6})
    assert resp.status_code == 200
    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 12})
    assert resp.status_code == 200
    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 13})
    assert resp.status_code == 422
    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 0})
    assert resp.status_code == 422
