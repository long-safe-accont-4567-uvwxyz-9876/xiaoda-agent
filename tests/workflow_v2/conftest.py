"""Task 6 fixtures: minimal FastAPI app + in-memory fake workflow-v2 service.

Per Task 6 rulings (R4/R5): tests must NOT import web.server.create_app
(it pulls the whole production app: DB, middleware, lifespan). Instead we
build a minimal app, override the auth dependency (established project
pattern, see tests/test_provider_onboarding.py), include the workflows-v2
router under /api/v1, register an HTTPException -> Envelope handler, and
inject a fake service via app.state.workflow_v2.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from web.routers.auth import get_current_user
from web.routers.workflows_v2 import router as workflows_v2_router


async def _http_exception_to_envelope(request: Request, exc: HTTPException) -> JSONResponse:
    """Convert HTTPException into the Envelope error shape.

    The production global handler (web/error_handler.py) only handles
    AppException, so the workflows-v2 router raises HTTPException with a
    dict detail and this test-app handler turns it into
    {"ok": False, "error": {"code": ..., "message": ...}, "data": None}.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", "ERROR")
        message = detail.get("message", str(exc))
    else:
        code, message = "ERROR", str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": {"code": code, "message": message}, "data": None},
    )


class FakeWorkflowV2Service:
    """In-memory stand-in for the workflow-v2 service (no DB).

    The router only talks to this via ``request.app.state.workflow_v2`` and
    awaits each method, so every method is async.
    """

    def __init__(self) -> None:
        self.definitions: dict[str, dict] = {}
        self.revisions: dict[str, dict[str, dict]] = {}
        self.runs: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self._idem: dict[str, str] = {}  # idempotency_key -> run_id
        self._run_counter = 0

    # --- definitions ---
    async def create_definition(self, body: dict) -> dict | None:
        wf_id = (body.get("id") or "").strip()
        if not wf_id or wf_id in self.definitions:
            return None
        definition = {
            "workflow_id": wf_id,
            "name": body.get("name", ""),
            "description": body.get("description", ""),
            "enabled": body.get("enabled", True),
            "current_revision_id": None,
            "etag": f"etag-{wf_id}-1",
        }
        self.definitions[wf_id] = definition
        return dict(definition)

    async def get_definition(self, wf_id: str) -> dict | None:
        d = self.definitions.get(wf_id)
        return dict(d) if d else None

    async def patch_definition(self, wf_id: str, body: dict) -> dict:
        d = self.definitions[wf_id]
        for key in ("name", "description", "enabled"):
            if key in body:
                d[key] = body[key]
        d["etag"] = f"etag-{wf_id}-2"
        return dict(d)

    async def add_revision(self, wf_id: str, revision: dict) -> dict:
        rev_id = revision.get("revision_id") or f"rev-{len(self.revisions.get(wf_id, {})) + 1}"
        stored = dict(revision)
        stored["revision_id"] = rev_id
        stored["workflow_id"] = wf_id
        stored.setdefault("content_hash", "fake-hash")
        stored.setdefault("created_at", time.time())
        self.revisions.setdefault(wf_id, {})[rev_id] = stored
        return dict(stored)

    async def publish_revision(self, wf_id: str, rev: str) -> dict | None:
        if wf_id not in self.definitions or rev not in self.revisions.get(wf_id, {}):
            return None
        self.definitions[wf_id]["current_revision_id"] = rev
        return dict(self.definitions[wf_id])

    # --- runs ---
    async def create_or_get_run(self, wf_id: str, input_: dict, idempotency_key: str) -> dict | None:
        if idempotency_key and idempotency_key in self._idem:
            run_id = self._idem[idempotency_key]
            return dict(self.runs[run_id])
        if wf_id not in self.definitions:
            return None
        self._run_counter += 1
        run_id = f"run-{self._run_counter}"
        run = {
            "run_id": run_id,
            "workflow_id": wf_id,
            "revision_id": self.definitions[wf_id].get("current_revision_id"),
            "status": "queued",
            "input": input_,
            "idempotency_key": idempotency_key,
            "last_seq": 1,
        }
        self.runs[run_id] = run
        self.events[run_id] = [
            {"run_id": run_id, "seq": 1, "event_type": "run_queued",
             "run_status": "queued", "timestamp": time.time()}
        ]
        if idempotency_key:
            self._idem[idempotency_key] = run_id
        return dict(run)

    async def snapshot(self, run_id: str) -> dict | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        snap = dict(run)
        snap["last_seq"] = len(self.events.get(run_id, []))
        return snap

    async def events_after(self, run_id: str, after_seq: int) -> list[dict]:
        return [dict(e) for e in self.events.get(run_id, []) if e["seq"] > after_seq]

    async def request_cancel(self, run_id: str) -> dict | None:
        if run_id not in self.runs:
            return None
        self.runs[run_id]["status"] = "cancelling"
        return dict(self.runs[run_id])


@pytest.fixture
def fake_service() -> FakeWorkflowV2Service:
    return FakeWorkflowV2Service()


@pytest.fixture
def app(fake_service: FakeWorkflowV2Service) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.add_exception_handler(HTTPException, _http_exception_to_envelope)
    app.include_router(workflows_v2_router, prefix="/api/v1")
    app.state.workflow_v2 = fake_service
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    # value is irrelevant: get_current_user is dependency-overridden
    return {"Authorization": "Bearer test"}


@pytest.fixture
def seeded_definition(fake_service: FakeWorkflowV2Service) -> dict:
    fake_service.definitions["w1"] = {
        "workflow_id": "w1",
        "name": "wf",
        "description": "",
        "enabled": True,
        "current_revision_id": None,
        "etag": "current-etag",
    }
    return dict(fake_service.definitions["w1"])
