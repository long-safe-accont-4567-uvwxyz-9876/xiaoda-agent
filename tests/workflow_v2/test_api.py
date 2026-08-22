"""Task 6: REST API layer tests (auth, ETag, idempotency).

Router mounted at /api/v1 (web/server.py mounts every router with that
prefix; the router file itself carries no prefix). Tests use the minimal
app built in tests/workflow_v2/conftest.py with the real WorkflowV2Service
over an in-memory SQLite repo — never web.server.create_app. The auth
dependency is overridden, so any Authorization header value is accepted.
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from httpx import AsyncClient

from workflow_v2.repository import WorkflowRepository


async def test_run_without_idempotency_key_auto_generates(client: AsyncClient, seeded_definition, auth_headers):
    """转正契约：前端不传 Idempotency-Key 时服务端自动生成 auto-* 键，仍 200 幂等。

    转正前要求调用方必须带 Idempotency-Key（缺省 400）；转正后 WebUI
    「启动」按钮不传该头，headers 由服务端兜底生成，每次各不相同。
    """
    r = await client.post("/api/v1/workflows/w1/runs", headers=auth_headers, json={"input": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["error"] is None
    assert body["data"]["idempotency_key"].startswith("auto-")
    assert body["data"]["status"] == "queued"

    r2 = await client.post("/api/v1/workflows/w1/runs", headers=auth_headers, json={"input": {}})
    assert r2.status_code == 200
    assert r2.json()["data"]["run_id"] != body["data"]["run_id"]  # 两次 auto 键互不相同

    # 显式带键的请求仍按旧契约幂等：同键同 run
    h = {**auth_headers, "Idempotency-Key": "key-1"}
    r3 = await client.post("/api/v1/workflows/w1/runs", headers=h, json={"input": {"a": 1}})
    r4 = await client.post("/api/v1/workflows/w1/runs", headers=h, json={"input": {"a": 1}})
    assert r3.status_code == 200 and r4.status_code == 200
    assert r3.json()["data"]["run_id"] == r4.json()["data"]["run_id"]


async def test_run_auto_migrates_v1_on_first_run(client: AsyncClient, auth_headers, tmp_path, monkeypatch):
    """首次点「启动」：workspace 只有 v1 JSON → POST runs 自动迁移并发布版本。

    覆盖转正闭环：ensure_published（v1 文件 → definition + revision）→
    create_run（带服务端 auto Idempotency-Key）→ 版本/记录列表可见。
    """
    import json

    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "w9.json").write_text(json.dumps({
        "id": "w9", "name": "自动迁移",
        "nodes": [{"id": "c", "type": "custom", "label": "自由文本", "note": "今天天气怎么样"},
                  {"id": "s", "type": "step", "label": "小结", "note": "再总结一下"}],
    }), encoding="utf-8")
    monkeypatch.setattr("config.WORKSPACE_DIR", str(tmp_path), raising=False)

    r = await client.post("/api/v1/workflows/w9/runs", headers=auth_headers, json={"input": {}})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["workflow_id"] == "w9"
    assert data["revision_id"], "自动迁移应产生 current revision"
    assert data["status"] == "queued"

    revs = await client.get("/api/v1/workflows/w9/revisions", headers=auth_headers)
    assert revs.status_code == 200, revs.text
    assert len(revs.json()["data"]) == 1

    runs = await client.get("/api/v1/workflows/w9/runs", headers=auth_headers)
    assert runs.status_code == 200
    listed = runs.json()["data"]
    assert any(x["run_id"] == data["run_id"] for x in listed)


async def test_patch_definition_etag_conflict(client: AsyncClient, seeded_definition, auth_headers):
    """Brief test 2: mismatched If-Match -> 409 ETAG_CONFLICT (definition untouched)."""
    h = {**auth_headers, "If-Match": "stale-etag"}
    r = await client.patch("/api/v1/workflows/w1", headers=h, json={"name": "new"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ETAG_CONFLICT"
    assert r.json()["ok"] is False


async def test_patch_definition_missing_if_match_conflict(client: AsyncClient, seeded_definition, auth_headers):
    """Brief test: PATCH with NO If-Match header -> 409 ETAG_CONFLICT (same as mismatch)."""
    r = await client.patch("/api/v1/workflows/w1", headers=auth_headers, json={"name": "new"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ETAG_CONFLICT"
    assert r.json()["ok"] is False


async def test_patch_definition_cas_atomic(client: AsyncClient, app: FastAPI, repo: WorkflowRepository,
                                          seeded_definition, auth_headers):
    """CAS: a concurrent write between the etag read and the UPDATE must be detected.

    Simulates the TOCTOU window: the route/service reads etag 'etag-abc' (matches the
    If-Match header), then another client bumps the etag before patch_definition's
    UPDATE runs. A blind update would overwrite it -> 200 (lost update); the atomic
    CAS must see 0 matched rows -> 409 ETAG_CONFLICT and leave the row untouched.
    """
    svc = app.state.workflow_v2
    original_get = svc.get_definition

    async def racing_get(wf_id: str):
        row = await original_get(wf_id)
        # concurrent writer lands between our read and our write
        await repo.conn.execute(
            "UPDATE wf_definition SET etag=?, updated_at=? WHERE workflow_id=?",
            ("etag-stolen", time.time(), wf_id),
        )
        await repo.conn.commit()
        return row

    svc.get_definition = racing_get  # instance-level shadow to open the race window
    h = {**auth_headers, "If-Match": "etag-abc"}
    r = await client.patch("/api/v1/workflows/w1", headers=h, json={"name": "new"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ETAG_CONFLICT"
    row = await repo.conn.execute(
        "SELECT etag, name FROM wf_definition WHERE workflow_id='w1'"
    )
    got = await row.fetchone()
    assert got["etag"] == "etag-stolen"  # the concurrent write survived, no lost update
    assert got["name"] == "wf"


async def test_create_run_happy_path_and_idempotency(client: AsyncClient, seeded_definition, auth_headers):
    """Happy path: create run with Idempotency-Key -> 200 Envelope; same key -> same run_id."""
    h = {**auth_headers, "Idempotency-Key": "key-1"}
    r1 = await client.post("/api/v1/workflows/w1/runs", headers=h, json={"input": {"a": 1}})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["ok"] is True and body1["error"] is None
    data1 = body1["data"]
    assert data1["run_id"] and data1["status"] == "queued"
    assert data1["workflow_id"] == "w1"
    assert data1["revision_id"] == "rev1"
    assert data1["idempotency_key"] == "key-1"
    assert data1["input"] == {"a": 1}

    # repeat with the same key -> same run, never a duplicate
    r2 = await client.post("/api/v1/workflows/w1/runs", headers=h, json={"input": {"a": 1}})
    assert r2.status_code == 200
    assert r2.json()["data"]["run_id"] == data1["run_id"]
