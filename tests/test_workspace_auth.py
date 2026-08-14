"""workspace 路由鉴权测试（P0 认证绕过修复验证）

验证 /api/v1/workspace/* 全部端点都要求 Bearer token 鉴权：
1. 单元测试：router 级别必须声明 get_current_user 鉴权依赖（与 insight 等路由一致）。
2. 集成测试：用最小 FastAPI app 挂载路由，未认证请求应返回 401。
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.routers.auth import get_current_user
from web.routers.workspace import router as workspace_router


def _declared_auth_callables(router):
    """提取 router.dependencies 中每个 Depends 指向的 callable。"""
    return [getattr(d, "dependency", None) for d in router.dependencies]


def test_workspace_router_declares_auth_dependency():
    """router 级别必须声明鉴权依赖，否则全部端点匿名可访问。"""
    assert workspace_router.dependencies, "workspace router 未声明任何 dependencies（认证绕过）"
    assert get_current_user in _declared_auth_callables(workspace_router), (
        "workspace router 的 dependencies 中未包含 get_current_user"
    )


@pytest.fixture
def client():
    """最小 FastAPI app，仅挂载 workspace 路由（避免全量 app 启动开销）。"""
    app = FastAPI()
    app.include_router(workspace_router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/v1/workspace", None),
    ("post", "/api/v1/workspace/confirm", {"path": "/tmp"}),
    ("delete", "/api/v1/workspace", None),
    ("get", "/api/v1/workspace/browse", None),
    ("get", "/api/v1/workspace/whitelist", None),
    ("post", "/api/v1/workspace/whitelist", {"command": "npm"}),
    ("post", "/api/v1/workspace/confirm_cmd", {
        "request_id": "req", "decision": "allow_once",
    }),
    ("get", "/api/v1/workspace/audit", None),
])
def test_workspace_endpoints_reject_unauthenticated(client, method, path, body):
    """未携带 Bearer token 的请求应被 401 拒绝（依赖在 body 校验之前执行）。"""
    fn = getattr(client, method)
    if body is None:
        r = fn(path)
    else:
        r = fn(path, json=body)
    assert r.status_code == 401, f"{method.upper()} {path} 应返回 401，实际 {r.status_code}"
