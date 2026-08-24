"""web/routers/metrics.py — /metrics 端点 token 鉴权测试 (VULN-08)

验证 /metrics 端点已叠加 get_current_user token 认证（在反代部署下修复公网暴露）：
1. 单元测试：router 级别必须声明 get_current_user 鉴权依赖。
2. 集成测试：未携带有效 token 的请求应返回 401，且不返回指标内容。
3. 集成测试：携带有效 token + localhost 仍返回 200 + 指标。
4. 集成测试：携带有效 token + 非 localhost 仍返回 403（保留 localhost-only 限制）。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.auth import _issue_token, get_current_user
from web.routers.metrics import router as metrics_router


def _declared_auth_callables(router):
    """提取 router.dependencies 中每个 Depends 指向的 callable。"""
    return [getattr(d, "dependency", None) for d in router.dependencies]


def test_metrics_router_declares_auth_dependency():
    """router 级别必须声明鉴权依赖，否则 /metrics 匿名可访问。"""
    assert metrics_router.dependencies, "metrics router 未声明任何 dependencies（认证绕过）"
    assert get_current_user in _declared_auth_callables(metrics_router), (
        "metrics router 的 dependencies 中未包含 get_current_user"
    )


@pytest.fixture
def app():
    """最小 FastAPI app，仅挂载 metrics 路由（避免全量 app 启动开销）。"""
    a = FastAPI()
    a.include_router(metrics_router)
    return a


@pytest.fixture
def local_client(app):
    """模拟本机回环访问（request.client.host = 127.0.0.1）。"""
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def lan_client(app):
    """模拟非 localhost 来源（局域网 IP）。"""
    return TestClient(app, client=("192.168.1.100", 50000))


def test_metrics_rejects_unauthenticated(local_client):
    """未携带 Bearer token 的请求应被 401 拒绝，且不泄露指标内容。"""
    r = local_client.get("/metrics")
    assert r.status_code == 401, f"未认证应 401，实际 {r.status_code}"
    body = r.text
    for leaked in ("process_cpu_seconds", "python_info", "tool_exec_success_total"):
        assert leaked not in body, f"未认证响应泄露了指标: {leaked}"


def test_metrics_rejects_invalid_token(local_client):
    """携带无效 token 的请求应被 401 拒绝。"""
    r = local_client.get("/metrics", headers={"Authorization": "Bearer invalid-token"})
    assert r.status_code == 401, f"无效 token 应 401，实际 {r.status_code}"


def test_metrics_allows_authenticated_localhost(local_client):
    """携带有效 token + localhost 请求仍正常返回指标。"""
    token, _ = _issue_token()
    r = local_client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"已认证 localhost 应 200，实际 {r.status_code} body={r.text[:200]}"
    assert "# TYPE" in r.text


def test_metrics_rejects_authenticated_non_localhost(lan_client):
    """携带有效 token + 非 localhost 请求仍受 localhost-only 限制，返回 403。"""
    token, _ = _issue_token()
    r = lan_client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, f"已认证非 localhost 应 403，实际 {r.status_code}"
    assert r.json() == {"error": "Forbidden"}
