"""Workflow V2 REST API (auth, ETag, idempotency, snapshot/replay).

Routed via ``request.app.state.workflow_v2`` — a service object injected at
startup (see R5). Endpoints listed in Task 6 brief; WebSocket stream and
signals are explicitly deferred to a later task and are NOT implemented here.

Note: this router carries NO path prefix (project convention — web/server.py
mounts every router under ``/api/v1``), so the real paths are e.g.
``/api/v1/workflows/{wf_id}/runs``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from workflow_v2.graph import GraphError, validate_graph
from workflow_v2.models import EdgeSpec, NodeSpec
from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["workflows-v2"], dependencies=[Depends(get_current_user)])


def _err(code: str, message: str, status: int, details: dict | None = None) -> None:
    raise HTTPException(status, detail={"code": code, "message": message, "details": details or {}})


# --- definitions -------------------------------------------------------------

@router.post("/workflows", response_model=Envelope[dict])
async def create_definition(body: dict, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    definition = await svc.create_definition(body)
    if definition is None:
        _err("WORKFLOW_EXISTS", "workflow already exists", 409)
    return Envelope(data=definition)


@router.patch("/workflows/{wf_id}", response_model=Envelope[dict])
async def patch_definition(wf_id: str, body: dict, request: Request,
                           if_match: str | None = Header(default=None, alias="If-Match")) -> Any:
    svc = request.app.state.workflow_v2
    current = await svc.get_definition(wf_id)
    if current is None:
        _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    if if_match is None or if_match != current["etag"]:
        _err("ETAG_CONFLICT", "definition was modified by another client", 409)
    updated = await svc.patch_definition(wf_id, body)
    return Envelope(data=updated)


# --- immutable revisions -------------------------------------------------------

@router.post("/workflows/{wf_id}/revisions", response_model=Envelope[dict])
async def create_revision(wf_id: str, body: dict, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    if await svc.get_definition(wf_id) is None:
        _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    try:
        nodes = [NodeSpec(**n) for n in body.get("nodes", [])]
        edges = [EdgeSpec(**e) for e in body.get("edges", [])]
    except (TypeError, ValueError) as exc:
        _err("WORKFLOW_REVISION_INVALID", f"invalid graph payload: {exc}", 400)
    try:
        validate_graph(nodes, edges)
    except GraphError as exc:
        _err("WORKFLOW_REVISION_INVALID", str(exc), 400, exc.details)
    revision = {
        "nodes": body.get("nodes", []),
        "edges": body.get("edges", []),
        "input_schema": body.get("input_schema", {}),
    }
    if body.get("revision_id"):
        revision["revision_id"] = body["revision_id"]
    stored = await svc.add_revision(wf_id, revision)
    return Envelope(data=stored)


@router.post("/workflows/{wf_id}/revisions/{rev}/publish", response_model=Envelope[dict])
async def publish_revision(wf_id: str, rev: str, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    updated = await svc.publish_revision(wf_id, rev)
    if updated is None:
        _err("WORKFLOW_NOT_FOUND", "workflow or revision not found", 404)
    return Envelope(data=updated)


# --- runs ----------------------------------------------------------------------

@router.post("/workflows/{wf_id}/runs", response_model=Envelope[dict])
async def create_run(wf_id: str, body: dict, request: Request,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Any:
    if not idempotency_key:
        _err("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required", 400)
    svc = request.app.state.workflow_v2
    run = await svc.create_or_get_run(wf_id, body.get("input", {}), idempotency_key)
    if run is None:
        _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    return Envelope(data=run)


@router.get("/workflow-runs/{run_id}", response_model=Envelope[dict])
async def get_run(run_id: str, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    snap = await svc.snapshot(run_id)
    if snap is None:
        _err("RUN_NOT_FOUND", "run not found", 404)
    return Envelope(data=snap)


@router.get("/workflow-runs/{run_id}/events", response_model=Envelope[list[dict]])
async def replay(run_id: str, request: Request, after_seq: int = 0) -> Any:
    svc = request.app.state.workflow_v2
    events = await svc.events_after(run_id, after_seq)
    return Envelope(data=events)


@router.post("/workflow-runs/{run_id}/cancel", response_model=Envelope[dict])
async def cancel(run_id: str, request: Request) -> Any:
    svc = request.app.state.workflow_v2
    result = await svc.request_cancel(run_id)  # idempotent
    if result is None:
        _err("RUN_NOT_FOUND", "run not found", 404)
    return Envelope(data=result)
