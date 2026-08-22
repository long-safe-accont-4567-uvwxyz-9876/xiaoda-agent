"""Task 6 fixtures: minimal FastAPI app over the REAL workflow-v2 service.

Per Task 6 rulings (R4/R5): tests must NOT import web.server.create_app
(it pulls the whole production app: DB, middleware, lifespan). Instead we
build a minimal app, override the auth dependency (established project
pattern, see tests/test_provider_onboarding.py), include the workflows-v2
router under /api/v1, and inject the real WorkflowV2Service backed by an
in-memory SQLite repository via app.state.workflow_v2.
"""
from __future__ import annotations

import time

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db.db_workflow import create_schema
from web.routers.auth import get_current_user
from web.routers.workflows_v2 import router as workflows_v2_router
from workflow_v2.repository import WorkflowRepository
from workflow_v2.service import WorkflowV2Service


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Dummy auth header.

    The router is auth-gated via dependencies=[Depends(get_current_user)];
    the app fixture overrides get_current_user, so any header value is fine
    and no real token issuance happens.
    """
    return {"Authorization": "Bearer x"}


@pytest.fixture
async def repo() -> WorkflowRepository:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    yield WorkflowRepository(conn)
    await conn.close()


@pytest.fixture
async def app(repo: WorkflowRepository) -> FastAPI:
    app = FastAPI()
    svc = WorkflowV2Service(repo)
    # M3 灰度门控：默认打开全局开关，保证既有运行/发布类测试不受门控影响；
    # 灰度语义专项测试各自显式 set_config 关闭/白名单
    await svc.set_config("workflow_v2.enabled", True)
    app.state.workflow_v2 = svc
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.include_router(workflows_v2_router, prefix="/api/v1")
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
async def seeded_definition(repo: WorkflowRepository) -> dict:
    """Insert a wf_definition row (etag 'etag-abc') plus its wf_revision row."""
    now = time.time()
    await repo.conn.execute(
        "INSERT INTO wf_definition(workflow_id, name, description, enabled,"
        " current_revision_id, etag, created_at, updated_at)"
        " VALUES('w1','wf','',1,'rev1','etag-abc',?,?)",
        (now, now),
    )
    await repo.conn.execute(
        "INSERT INTO wf_revision(revision_id, workflow_id, graph_json, content_hash, created_at)"
        " VALUES('rev1','w1','{}','hash',?)",
        (now,),
    )
    await repo.conn.commit()
    return {"workflow_id": "w1", "etag": "etag-abc", "current_revision_id": "rev1"}
