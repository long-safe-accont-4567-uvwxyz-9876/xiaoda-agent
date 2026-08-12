"""Workflow V2 REST API (auth, ETag, idempotency, snapshot/replay).

Routed via ``request.app.state.workflow_v2`` — a ``WorkflowV2Service``
instance injected at startup (wiring into web/server.py is a documented
follow-up; tests inject it directly).

Note: this router carries NO path prefix (project convention — web/server.py
mounts every router under ``/api/v1``), so the real paths are e.g.
``/api/v1/workflows/{wf_id}/runs``.

WebSocket ``/stream`` and ``POST .../signals/{node_id}`` are explicitly
deferred to a later task and are NOT implemented here.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from web.routers.auth import get_current_user
from web.schemas import Envelope, ErrorDetail

router = APIRouter(tags=["workflows-v2"], dependencies=[Depends(get_current_user)])


def _err(code: str, message: str, status: int) -> JSONResponse:
    """Error responses MUST use the Envelope shape (plan global constraint).

    We deliberately return a JSONResponse instead of raising HTTPException:
    web/error_handler.py only converts AppException, so an HTTPException here
    would not be turned into an Envelope by the production app.
    """
    return JSONResponse(
        status_code=status,
        content=Envelope(ok=False, data=None, error=ErrorDetail(code=code, message=message)).model_dump(),
    )


@router.post("/workflows/{wf_id}/runs", response_model=Envelope[dict])
async def create_run(wf_id: str, body: dict, request: Request,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Any:
    if not idempotency_key:
        return _err("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
    svc = request.app.state.workflow_v2
    run = await svc.create_or_get_run(wf_id, body.get("input", {}), idempotency_key)
    if run is None:
        return _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    return Envelope(data=run)


@router.patch("/workflows/{wf_id}", response_model=Envelope[dict])
async def patch_definition(wf_id: str, body: dict, request: Request,
                           if_match: str | None = Header(default=None, alias="If-Match")) -> Any:
    svc = request.app.state.workflow_v2
    current = await svc.get_definition(wf_id)
    if current is None:
        return _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    if if_match is None or if_match != current["etag"]:
        return _err("ETAG_CONFLICT", "definition was modified by another client", 409)
    updated = await svc.patch_definition(wf_id, body)
    return Envelope(data=updated)


@router.get("/workflow-runs/{run_id}", response_model=Envelope[dict])
async def get_run(run_id: str, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    snap = await svc.snapshot(run_id)
    if snap is None:
        return _err("RUN_NOT_FOUND", "run not found", 404)
    return Envelope(data=snap)


@router.get("/workflow-runs/{run_id}/events", response_model=Envelope[list[dict]])
async def replay(run_id: str, request: Request, after_seq: int = 0) -> Any:
    svc = request.app.state.workflow_v2
    events = await svc.events_after(run_id, after_seq)
    return Envelope(data=events)


@router.post("/workflow-runs/{run_id}/cancel", response_model=Envelope[dict])
async def cancel(run_id: str, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    try:
        result = await svc.request_cancel(run_id)  # idempotent
    except KeyError:
        return _err("RUN_NOT_FOUND", "run not found", 404)
    return Envelope(data=result)
