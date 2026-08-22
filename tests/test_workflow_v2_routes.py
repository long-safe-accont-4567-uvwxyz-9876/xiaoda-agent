# tests/test_workflow_v2_routes.py
"""M2 路由层验收：发布 → 存档 → 版本列表 → 回滚（etag CAS）全链路走 HTTP。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.db_workflow import create_schema
from web.routers.auth import get_current_user
from web.routers.workflows_v2 import router as workflows_v2_router
from workflow_v2.repository import WorkflowRepository
from workflow_v2.service import WorkflowV2Service


def _v1(name: str, label: str) -> dict:
    return {
        "id": name, "name": name, "description": "",
        "version": "1.0.0", "enabled": True,
        "nodes": [{"id": "n1", "type": "tool", "label": label,
                   "ref": "web_search", "note": "", "params": {"q": 1}}],
        "edges": [],
    }


@pytest.fixture()
def app(tmp_path):
    db_path = str(tmp_path / "wf_routes.db")
    workspace = tmp_path / "workflows"
    workspace.mkdir()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await create_schema(conn)
        svc = WorkflowV2Service(WorkflowRepository(conn))
        svc._v1_path = lambda wf_id: (
            (workspace / f"{wf_id}.json") if (workspace / f"{wf_id}.json").exists() else None)
        app.state.workflow_v2 = svc
        yield
        await conn.close()

    application = FastAPI(lifespan=_lifespan)
    application.dependency_overrides[get_current_user] = lambda: "test-user"
    application.include_router(workflows_v2_router, prefix="/api/v1")
    return application, workspace


def test_publish_rollback_full_loop(app, tmp_path):
    app, workspace = app
    (workspace / "wf1.json").write_text(json.dumps(_v1("wf1", "v1")), encoding="utf-8")
    with TestClient(app) as c:
        # 发布 v1 → 第一条版本置为当前
        r = c.post("/api/v1/workflows/wf1/publish")
        assert r.status_code == 200, r.text
        rev1 = r.json()["data"]["revision"]["revision_id"]

        # 改 v1 文件再发布 → rev2 成为当前
        (workspace / "wf1.json").write_text(json.dumps(_v1("wf1", "v2")), encoding="utf-8")
        r = c.post("/api/v1/workflows/wf1/publish")
        rev2 = r.json()["data"]["revision"]["revision_id"]
        assert rev2 != rev1

        rows = c.get("/api/v1/workflows/wf1/revisions").json()["data"]
        assert len(rows) == 2
        by_id = {x["revision_id"]: x for x in rows}
        assert by_id[rev2]["current"] is True
        etag = by_id[rev2]["etag"]

        # 无 If-Match → 409
        r = c.patch("/api/v1/workflows/wf1/current", json={"revision_id": rev1})
        assert r.status_code == 409

        # 错误的 revision → 404
        r = c.patch("/api/v1/workflows/wf1/current", json={"revision_id": "no_such"},
                    headers={"If-Match": etag})
        assert r.status_code == 404

        # 回滚成功 → current=rev1、新 etag、revision 列表 current 标记翻转
        r = c.patch("/api/v1/workflows/wf1/current", json={"revision_id": rev1},
                    headers={"If-Match": etag})
        assert r.status_code == 200, r.text
        updated = r.json()["data"]
        assert updated["current_revision_id"] == rev1
        assert updated["etag"] != etag
        rows = c.get("/api/v1/workflows/wf1/revisions").json()["data"]
        by_id = {x["revision_id"]: x for x in rows}
        assert by_id[rev1]["current"] is True
        assert by_id[rev2]["current"] is False

        # 旧 etag 再次回滚 → 409（CAS）
        r = c.patch("/api/v1/workflows/wf1/current", json={"revision_id": rev2},
                    headers={"If-Match": etag})
        assert r.status_code == 409


def test_snapshot_revision_does_not_promote(app, tmp_path):
    app, workspace = app
    (workspace / "wf1.json").write_text(json.dumps(_v1("wf1", "x")), encoding="utf-8")
    with TestClient(app) as c:
        r = c.post("/api/v1/workflows/wf1/revisions", json={})
        assert r.status_code == 200, r.text
        rows = c.get("/api/v1/workflows/wf1/revisions").json()["data"]
        assert rows and rows[0]["current"] is False    # 存档不置当前
        assert rows[0]["etag"]  # 定义行自动创建，etag 存在


def test_engine_unavailable_returns_503():
    """未装配（降级模式）：app.state.workflow_v2 缺失 → 统一 503。"""
    application = FastAPI()
    application.dependency_overrides[get_current_user] = lambda: "test-user"
    application.include_router(workflows_v2_router, prefix="/api/v1")
    with TestClient(application) as c:
        r = c.post("/api/v1/workflows/wf1/revisions", json={})
        assert r.status_code == 503
        r = c.patch("/api/v1/workflows/wf1/current", json={"revision_id": "r"},
                    headers={"If-Match": "etag-x"})
        assert r.status_code == 503
