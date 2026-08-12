"""Task 6: REST API layer tests (auth, ETag, idempotency).

Router mounted at /api/v1 (web/server.py mounts every router with that
prefix; the router file itself carries no prefix). Tests use the minimal
app built in tests/workflow_v2/conftest.py with the real WorkflowV2Service
over an in-memory SQLite repo — never web.server.create_app. The auth
dependency is overridden, so any Authorization header value is accepted.
"""
from __future__ import annotations

from httpx import AsyncClient

_AUTH = {"Authorization": "Bearer test"}


async def test_run_requires_idempotency_key(client: AsyncClient):
    """Brief test 1: missing Idempotency-Key header -> 400 IDEMPOTENCY_KEY_REQUIRED."""
    r = await client.post("/api/v1/workflows/w1/runs", headers=_AUTH, json={"input": {}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert r.json()["ok"] is False and r.json()["data"] is None


async def test_patch_definition_etag_conflict(client: AsyncClient, seeded_definition):
    """Brief test 2: mismatched If-Match -> 409 ETAG_CONFLICT (definition untouched)."""
    h = {**_AUTH, "If-Match": "stale-etag"}
    r = await client.patch("/api/v1/workflows/w1", headers=h, json={"name": "new"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ETAG_CONFLICT"
    assert r.json()["ok"] is False


async def test_create_run_happy_path_and_idempotency(client: AsyncClient, seeded_definition):
    """Happy path: create run with Idempotency-Key -> 200 Envelope; same key -> same run_id."""
    h = {**_AUTH, "Idempotency-Key": "key-1"}
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
