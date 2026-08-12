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


async def test_run_requires_idempotency_key(client: AsyncClient, auth_headers):
    """Brief test 1: missing Idempotency-Key header -> 400 IDEMPOTENCY_KEY_REQUIRED."""
    r = await client.post("/api/v1/workflows/w1/runs", headers=auth_headers, json={"input": {}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert r.json()["ok"] is False and r.json()["data"] is None


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
