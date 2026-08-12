"""Task 6: REST API layer tests (auth, ETag, idempotency, snapshot/replay).

Router mounted at /api/v1 (web/server.py mounts every router with that
prefix; the router file itself carries no prefix). Tests use the minimal
app built in tests/workflow_v2/conftest.py — never web.server.create_app.
"""
from __future__ import annotations

from httpx import AsyncClient


# --- helpers --------------------------------------------------------------

async def _create_workflow(c: AsyncClient, auth_headers: dict, wf_id: str = "w1") -> dict:
    r = await c.post(
        "/api/v1/workflows",
        headers=auth_headers,
        json={"id": wf_id, "name": "wf", "description": "d"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _add_revision(c: AsyncClient, auth_headers: dict, wf_id: str = "w1") -> dict:
    body = {
        "nodes": [
            {"id": "start", "type": "start", "name": "start"},
            {"id": "end", "type": "end", "name": "end"},
        ],
        "edges": [{"source": "start", "target": "end"}],
        "input_schema": {},
    }
    r = await c.post(f"/api/v1/workflows/{wf_id}/revisions", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _publish_revision(c: AsyncClient, auth_headers: dict, rev_id: str, wf_id: str = "w1") -> dict:
    r = await c.post(f"/api/v1/workflows/{wf_id}/revisions/{rev_id}/publish", headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _start_run(c: AsyncClient, auth_headers: dict, key: str = "k1", wf_id: str = "w1") -> dict:
    h = {**auth_headers, "Idempotency-Key": key}
    r = await c.post(f"/api/v1/workflows/{wf_id}/runs", headers=h, json={"input": {"a": 1}})
    assert r.status_code == 200, r.text
    return r.json()["data"]


# --- the brief's two tests -------------------------------------------------

async def test_run_requires_idempotency_key(client, auth_headers):
    r = await client.post("/api/v1/workflows/w1/runs", headers=auth_headers, json={"input": {}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert r.json()["ok"] is False and r.json()["data"] is None


async def test_patch_definition_etag_conflict(client, auth_headers, seeded_definition):
    h = {**auth_headers, "If-Match": "stale-etag"}
    r = await client.patch("/api/v1/workflows/w1", headers=h, json={"name": "new"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ETAG_CONFLICT"


# --- definitions -----------------------------------------------------------

async def test_create_definition(client, auth_headers):
    data = await _create_workflow(client, auth_headers, "w9")
    assert data["workflow_id"] == "w9"
    assert data["name"] == "wf"
    assert data["etag"]


async def test_patch_definition_success_with_matching_etag(client, auth_headers, seeded_definition):
    h = {**auth_headers, "If-Match": "current-etag"}
    r = await client.patch("/api/v1/workflows/w1", headers=h, json={"name": "renamed"})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "renamed"


async def test_patch_definition_etag_header_missing(client, auth_headers, seeded_definition):
    r = await client.patch("/api/v1/workflows/w1", headers=auth_headers, json={"name": "new"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ETAG_CONFLICT"


async def test_patch_definition_not_found(client, auth_headers):
    r = await client.patch("/api/v1/workflows/nope", headers={**auth_headers, "If-Match": "x"},
                           json={"name": "new"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


# --- revisions + publish ----------------------------------------------------

async def test_create_revision_valid_and_publish(client, auth_headers):
    await _create_workflow(client, auth_headers)
    rev = await _add_revision(client, auth_headers)
    assert rev["revision_id"] and rev["workflow_id"] == "w1"
    published = await _publish_revision(client, auth_headers, rev["revision_id"])
    assert published["current_revision_id"] == rev["revision_id"]


async def test_create_revision_rejects_invalid_graph(client, auth_headers):
    await _create_workflow(client, auth_headers)
    body = {
        "nodes": [
            {"id": "start", "type": "start", "name": "start"},
            {"id": "end", "type": "end", "name": "end"},
        ],
        "edges": [{"source": "start", "target": "ghost"}],  # dangling edge
    }
    r = await client.post("/api/v1/workflows/w1/revisions", headers=auth_headers, json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "WORKFLOW_REVISION_INVALID"


async def test_create_revision_workflow_not_found(client, auth_headers):
    r = await client.post(
        "/api/v1/workflows/nope/revisions",
        headers=auth_headers,
        json={"nodes": [], "edges": []},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


# --- runs: idempotency, snapshot, replay, cancel -----------------------------

async def test_create_run_idempotent_replay(client, auth_headers):
    await _create_workflow(client, auth_headers)
    rev = await _add_revision(client, auth_headers)
    await _publish_revision(client, auth_headers, rev["revision_id"])
    first = await _start_run(client, auth_headers, key="k1")
    second = await _start_run(client, auth_headers, key="k1")
    assert first["run_id"] == second["run_id"]
    assert second["revision_id"] == rev["revision_id"]


async def test_create_run_workflow_not_found(client, auth_headers):
    r = await client.post("/api/v1/workflows/nope/runs",
                          headers={**auth_headers, "Idempotency-Key": "k1"}, json={"input": {}})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


async def test_get_run_snapshot(client, auth_headers):
    await _create_workflow(client, auth_headers)
    run = await _start_run(client, auth_headers)
    r = await client.get(f"/api/v1/workflow-runs/{run['run_id']}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["run_id"] == run["run_id"]
    assert r.json()["data"]["last_seq"] == 1


async def test_get_run_not_found(client, auth_headers):
    r = await client.get("/api/v1/workflow-runs/ghost", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RUN_NOT_FOUND"


async def test_replay_events_after_seq(client, auth_headers):
    await _create_workflow(client, auth_headers)
    run = await _start_run(client, auth_headers)
    r = await client.get(f"/api/v1/workflow-runs/{run['run_id']}/events?after_seq=0",
                         headers=auth_headers)
    assert r.status_code == 200
    events = r.json()["data"]
    assert len(events) == 1 and events[0]["seq"] == 1
    r = await client.get(f"/api/v1/workflow-runs/{run['run_id']}/events?after_seq=1",
                         headers=auth_headers)
    assert r.json()["data"] == []


async def test_cancel_run_idempotent(client, auth_headers):
    await _create_workflow(client, auth_headers)
    run = await _start_run(client, auth_headers)
    r1 = await client.post(f"/api/v1/workflow-runs/{run['run_id']}/cancel", headers=auth_headers)
    r2 = await client.post(f"/api/v1/workflow-runs/{run['run_id']}/cancel", headers=auth_headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["data"]["status"] == "cancelling"


async def test_cancel_run_not_found(client, auth_headers):
    r = await client.post("/api/v1/workflow-runs/ghost/cancel", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RUN_NOT_FOUND"
