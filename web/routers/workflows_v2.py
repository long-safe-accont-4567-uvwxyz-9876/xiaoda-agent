"""Workflow V2 REST API (auth, ETag, idempotency, snapshot/replay).

Routed via ``request.app.state.workflow_v2`` — a ``WorkflowV2Service``
instance injected at startup by ``web/server.py`` (转正 2026-08-22：
此前只挂路由未注入 state，访问必抛 AttributeError；现由
``workflow_v2/app.py::build_runtime`` 装配。降级模式下 state 缺失 → 503)。

Note: this router carries NO path prefix (project convention — web/server.py
mounts every router under ``/api/v1``), so the real paths are e.g.
``/api/v1/workflows/{wf_id}/runs``.

转正后的 WebUI 契约（web/frontend 对应调用）：
- ``GET  /workflows/{wf_id}/runs``          —— 运行记录列表
- ``GET  /workflows/{wf_id}/revisions``     —— 版本列表（含 current 标记与定义 etag）
- ``POST /workflows/{wf_id}/revisions``     —— 显式快照：固化版本但不提升 current
- ``POST /workflows/{wf_id}/publish``       —— 把当前版本发布为新版本并置为当前
- ``PATCH /workflows/{wf_id}/current``      —— 回滚：切换 current 到历史版本（If-Match etag）
- ``GET  /workflows/{wf_id}/v2-status``     —— 灰度可用性（全局开关 + 试点白名单）
- ``POST /workflows/{wf_id}/runs``          —— 启动一次运行（首次自动从 v1 迁移）
- ``GET/POST /workflow-runs/...``  —— 快照 / 事件回放 / 取消（原已实现）

灰度（M3，立项书 §6）：``workflow_v2.enabled`` 全局默认关 + ``pilot_wf_ids``
白名单；不在可用范围的工作流 ``POST /runs`` 返回 503 WORKFLOW_V2_DISABLED，
driver 对其 QUEUED run 跳过调度（不消耗、不失败）。

仍 deferred 的端点（501 明示、不崩溃）：已存 revision 的发布
（POST /revisions/{rev}/publish）与结构化信号（发布−审批闭环留待后续）。
"""
from __future__ import annotations

import uuid
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


def _svc(request: Request) -> Any | None:
    """降级模式下 app.state.workflow_v2 不存在 —— 统一 503，不抛 AttributeError。"""
    return getattr(request.app.state, "workflow_v2", None)


@router.post("/workflows/{wf_id}/runs", response_model=Envelope[dict])
async def create_run(wf_id: str, body: dict, request: Request,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Any:
    """启动一次运行。

    转正行为：
    - 前端 v1 页面首次点「运行」时 definition 可能尚未迁移 —— 自动从
      workspace/workflows/{wf_id}.json 转换并发布一个版本，再创建 run；
    - Idempotency-Key 缺省时由服务端自动生成（前端不强约束，仍幂等
      重放保护：带同 key 的重复请求返回同一 run）；
    - 灰度门控（M3）：全局开关 或 试点白名单命中才放行，否则 503。
    """
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    if not await svc.is_wf_enabled(wf_id):
        return _err("WORKFLOW_V2_DISABLED",
                    "该工作流未开放灰度执行（全局开关关闭且不在试点白名单）", 503)
    if await svc.ensure_published(wf_id) is None:
        return _err("WORKFLOW_NOT_FOUND", "workflow not found (legacy JSON missing too)", 404)
    run = await svc.create_or_get_run(
        wf_id, body.get("input", {}), idempotency_key or f"auto-{uuid.uuid4().hex[:12]}"
    )
    if run is None:
        return _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    return Envelope(data=run)


@router.get("/workflows/{wf_id}/runs", response_model=Envelope[list[dict]])
async def list_runs(wf_id: str, request: Request) -> Any:
    """运行记录列表（WebUI 运行弹窗）。"""
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    return Envelope(data=await svc.list_runs(wf_id))


@router.get("/workflows/{wf_id}/v2-status", response_model=Envelope[dict])
async def v2_status(wf_id: str, request: Request) -> Any:
    """灰度可用性状态：WebUI 据此显示/隐藏「启动」按钮（M3 试点）。"""
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    global_on = await svc.v2_global_enabled()
    whitelisted = wf_id in await svc.v2_pilot_ids()
    return Envelope(data={
        "enabled": global_on or whitelisted,
        "global_enabled": global_on,
        "whitelisted": whitelisted,
    })


@router.get("/workflows/{wf_id}/revisions", response_model=Envelope[list[dict]])
async def list_revisions(wf_id: str, request: Request) -> Any:
    """版本列表（WebUI 版本弹窗）。"""
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    return Envelope(data=await svc.list_revisions(wf_id))


@router.post("/workflows/{wf_id}/publish", response_model=Envelope[dict])
async def publish(wf_id: str, request: Request) -> Any:
    """发布新版本：把当前 v1 定义固化为新的不可变 revision（WebUI 发布按钮）。"""
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    published = await svc.publish_from_v1(wf_id)
    if published is None:
        return _err("WORKFLOW_NOT_FOUND", "workflow not found", 404)
    return Envelope(data=published)


@router.patch("/workflows/{wf_id}", response_model=Envelope[dict])
async def patch_definition(wf_id: str, body: dict, request: Request,
                           if_match: str | None = Header(default=None, alias="If-Match")) -> Any:
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    current = await svc.get_definition(wf_id)
    if current is None:
        return _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    if if_match is None:
        return _err("ETAG_CONFLICT", "If-Match header is required", 409)
    # Atomic CAS: the etag guard lives inside the UPDATE (service), so two
    # concurrent PATCHes with the same If-Match cannot both win the race.
    updated = await svc.patch_definition(wf_id, body, etag=if_match)
    if updated is None:
        return _err("ETAG_CONFLICT", "definition was modified by another client", 409)
    return Envelope(data=updated)


@router.get("/workflow-runs/{run_id}", response_model=Envelope[dict])
async def get_run(run_id: str, request: Request) -> Any:
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    snap = await svc.snapshot(run_id)
    if snap is None:
        return _err("RUN_NOT_FOUND", "run not found", 404)
    return Envelope(data=snap)


@router.get("/workflow-runs/{run_id}/events", response_model=Envelope[list[dict]])
async def replay(run_id: str, request: Request, after_seq: int = 0) -> Any:
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    events = await svc.events_after(run_id, after_seq)
    return Envelope(data=events)


@router.post("/workflow-runs/{run_id}/cancel", response_model=Envelope[dict])
async def cancel(run_id: str, request: Request) -> Any:
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    try:
        result = await svc.request_cancel(run_id)  # idempotent
    except KeyError:
        return _err("RUN_NOT_FOUND", "run not found", 404)
    return Envelope(data=result)


# --- deferred endpoints (501 stubs; see module docstring) -------------------

def _not_implemented(what: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content=Envelope(
            ok=False,
            data=None,
            error=ErrorDetail(
                code="NOT_IMPLEMENTED",
                message=f"{what} is deferred to a follow-up task",
            ),
        ).model_dump(),
    )


@router.post("/workflows/{wf_id}/revisions", response_model=Envelope[dict])
async def create_revision(wf_id: str, request: Request,
                          body: dict | None = None) -> Any:  # noqa: E501
    """显式创建版本：把当前定义固化为新的不可变 revision（不提升 current）。

    M2 语义（立项书 §4）："存档"动作——只快照不动运行版本；
    发布（POST /publish）= 快照 + 置为当前。
    """
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    snap = await svc.snapshot_revision_from_v1(wf_id)
    if snap is None:
        return _err("WORKFLOW_NOT_FOUND", "workflow not found (legacy JSON missing too)", 404)
    return Envelope(data=snap)


@router.patch("/workflows/{wf_id}/current", response_model=Envelope[dict])
async def rollback_current(wf_id: str, body: dict, request: Request,
                           if_match: str | None = Header(default=None, alias="If-Match")) -> Any:
    """回滚：把 current_revision_id 切到指定版本（版本不可变，只移动指针）。

    If-Match 必须携带定义当前 etag（版本列表已返回），CAS 语义与定义 PATCH
    一致：并发回滚/修改只能有一个赢。
    """
    svc = _svc(request)
    if svc is None:
        return _err("WF_RUNTIME_UNAVAILABLE", "工作流引擎未启动（降级模式）", 503)
    revision_id = (body or {}).get("revision_id")
    if not revision_id:
        return _err("REVISION_ID_REQUIRED", "revision_id is required", 422)
    if if_match is None:
        return _err("ETAG_CONFLICT", "If-Match header is required", 409)
    current = await svc.get_definition(wf_id)
    if current is None:
        return _err("WORKFLOW_NOT_FOUND", "definition not found", 404)
    updated = await svc.set_revision_current(wf_id, revision_id, etag=if_match)
    if updated is None:
        if not await svc.revision_exists(wf_id, revision_id):
            return _err("REVISION_NOT_FOUND", "revision not found for this workflow", 404)
        return _err("ETAG_CONFLICT", "definition was modified by another client", 409)
    return Envelope(data=updated)  # 定义新 etag 一并返回，前端后续 CAS 继续有效


@router.post("/workflow-runs/{run_id}/signals/{node_id}", response_model=Envelope[dict])
async def send_signal(run_id: str, node_id: str, body: dict, request: Request) -> Any:
    """Structured signal + signal_token — DEFERRED (501 stub)."""
    return _not_implemented("structured signal delivery")
