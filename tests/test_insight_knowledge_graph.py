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
    entities = [
        {"name": "小妲", "kind": "person"},
        {"name": "爸爸", "kind": "person"},
        {"name": "须弥", "kind": "place"},
        {"name": "孤岛", "kind": "place"},
    ]
    relations = [
        {"from_entity": "小妲", "relation_type": "家人", "to_entity": "爸爸", "confidence": 1.0},
        {"from_entity": "小妲", "relation_type": "居住", "to_entity": "须弥", "confidence": 0.9},
        {"from_entity": "爸爸", "relation_type": "到访", "to_entity": "须弥", "confidence": 0.5},
        {"from_entity": "孤岛", "relation_type": "位于", "to_entity": "须弥", "confidence": 0.4},
    ]
    fake = _FakeKnowledgeDB(entities, relations)

    app = FastAPI()
    app.include_router(insight.router)

    class _Core:
        class db:  # noqa: N801
            knowledge = fake

            @staticmethod
            async def fetch_all(sql, *a, **kw):
                # 全图概览路径：返回最近关系
                return list(reversed(relations[:80]))

    app.state.core = _Core()
    return TestClient(app)


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


def test_graph_entity_focus_edge_cap(client, monkeypatch):
    """关系数超过每层上限时截断到 400 且不崩溃。"""
    _auth(client)
    many = [
        {"from_entity": "小妲", "relation_type": f"r{i}", "to_entity": f"e{i}",
         "confidence": i / 1000}
        for i in range(500)
    ]
    ents = [{"name": f"e{i}", "kind": ""} for i in range(500)]
    fake = _FakeKnowledgeDB([{"name": "小妲", "kind": "person"}] + ents, many)

    class _Core:
        class db:  # noqa: N801
            knowledge = fake

            @staticmethod
            async def fetch_all(sql, *a, **kw):
                return []

    client.app.state.core = _Core()
    resp = client.get("/insight/knowledge/graph", params={"entity": "小妲", "depth": 1})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["edges"]) <= 400
    # 截断按 confidence 降序：保留的应是 confidence 最高的那批
    kept_conf = sorted((e["relation"] for e in data["edges"]))
    assert kept_conf[-1] == "r499"


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
