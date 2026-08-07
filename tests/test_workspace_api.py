"""workspace API 路由集成测试

响应格式为 Envelope: {"ok": true, "data": {...}}
测试中用 D(r) 辅助函数解包 data 字段。

安全回归覆盖：
- 所有 workspace 端点应在缺少 Bearer token 时返回 401（认证强保护）
- 认证用户的所有功能继续工作（不破坏功能性）
- browse_directory 应使用 realpath 规范化路径（防符号链接/.. 段绕过）
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import get_permission_manager
from web.routers.auth import get_current_user as _auth_get_current_user
from web.routers.workspace import (router as workspace_router,
                                    register_cmd_decision_scope,
                                    _pending_cmd_decisions)


def D(r):
    """解包 Envelope 响应的 data 字段"""
    return r.json()["data"]


@pytest.fixture
def app():
    """最小 FastAPI app，仅挂载 workspace 路由（避免全量 app 启动开销）"""
    app = FastAPI()
    app.include_router(workspace_router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app):
    """不注入认证的 TestClient：用于安全测试（验证未认证 → 401）"""
    return TestClient(app)


async def _fake_authed_user():
    """认证替身：返回固定 user_id，用于功能性测试绕过鉴权层。"""
    return "test-user"


@pytest.fixture
def authed_client(app):
    """注入了 authenticated user 的 TestClient：用于功能性回归。"""
    app.dependency_overrides[_auth_get_current_user] = _fake_authed_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(_auth_get_current_user, None)


@pytest.fixture
def pm():
    pm = get_permission_manager()
    pm.clear_cwd()
    pm.set_whitelist([])
    # 清空审计环形缓冲：全局单例的 _audit_buffer 跨测试保留，
    # 其他测试（如 test_tool_executor_workspace::test_delete_action_classified）
    # 写入的条目会污染 test_get_audit_with_entries 的 len 断言。
    pm.clear_audit_log()
    return pm


# ============================================================
# 安全回归：认证强保护（P0 级，曾缺失导致完全认证绕过）
# 触发条件：任何未携带 Authorization: Bearer <token> 的请求
# 预期：所有 workspace 端点均返回 401，无任何端点泄露
# ============================================================
class TestWorkspaceAuthEnforced:
    """无 token 的请求应统一返回 401（P0 漏洞回归防护）"""

    def test_get_workspace_no_token_401(self, client):
        assert client.get("/api/v1/workspace").status_code == 401

    def test_confirm_workspace_no_token_401(self, client, tmp_path):
        r = client.post("/api/v1/workspace/confirm", json={"path": str(tmp_path)})
        assert r.status_code == 401

    def test_revoke_workspace_no_token_401(self, client):
        assert client.delete("/api/v1/workspace").status_code == 401

    def test_browse_no_token_401(self, client, tmp_path):
        r = client.get("/api/v1/workspace/browse", params={"path": str(tmp_path)})
        assert r.status_code == 401

    def test_get_whitelist_no_token_401(self, client):
        assert client.get("/api/v1/workspace/whitelist").status_code == 401

    def test_add_whitelist_no_token_401(self, client):
        r = client.post("/api/v1/workspace/whitelist", json={"command": "ls"})
        assert r.status_code == 401

    def test_remove_whitelist_no_token_401(self, client):
        r = client.delete("/api/v1/workspace/whitelist/ls")
        assert r.status_code == 401

    def test_confirm_cmd_no_token_401(self, client):
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "any", "decision": "allow",
        })
        assert r.status_code == 401

    def test_audit_no_token_401(self, client):
        r = client.get("/api/v1/workspace/audit", params={"limit": 1})
        assert r.status_code == 401


# ============================================================
# 功能性回归：认证用户的所有端点正常工作
# （使用 authed_client 注入认证替身）
# ============================================================
class TestWorkspaceEndpoints:
    def test_get_unauthorized(self, authed_client, pm):
        pm.clear_cwd()
        r = authed_client.get("/api/v1/workspace")
        assert r.status_code == 200
        data = D(r)
        assert data["authorized"] is False
        assert data["path"] == ""

    def test_confirm_authorization(self, authed_client, pm, tmp_path):
        r = authed_client.post("/api/v1/workspace/confirm", json={"path": str(tmp_path)})
        assert r.status_code == 200
        data = D(r)
        assert data["authorized"] is True
        assert data["path"] == str(tmp_path)
        assert "authorized_at" in data
        assert pm.is_cwd_authorized() is True

    def test_confirm_invalid_path(self, authed_client, pm):
        r = authed_client.post("/api/v1/workspace/confirm", json={"path": "/nonexistent/path/xyz"})
        assert r.status_code == 400

    def test_revoke(self, authed_client, pm, tmp_path):
        authed_client.post("/api/v1/workspace/confirm", json={"path": str(tmp_path)})
        r = authed_client.delete("/api/v1/workspace")
        assert r.status_code == 200
        assert D(r)["authorized"] is False
        assert pm.is_cwd_authorized() is False

    def test_browse_directory(self, authed_client, tmp_path):
        (tmp_path / "subdir1").mkdir()
        (tmp_path / "subdir2").mkdir()
        (tmp_path / "file.txt").write_text("hi")
        r = authed_client.get("/api/v1/workspace/browse", params={"path": str(tmp_path)})
        assert r.status_code == 200
        data = D(r)
        assert "subdir1" in data["dirs"]
        assert "subdir2" in data["dirs"]
        assert "file.txt" not in data["dirs"]

    def test_browse_invalid_path(self, authed_client):
        r = authed_client.get("/api/v1/workspace/browse", params={"path": "/nonexistent/xyz"})
        assert r.status_code == 400

    def test_browse_returns_parent(self, authed_client, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        r = authed_client.get("/api/v1/workspace/browse", params={"path": str(sub)})
        assert r.status_code == 200
        assert D(r)["parent"] is not None

    def test_browse_normalizes_dotdot(self, authed_client, tmp_path):
        """browse 路径应规范化解析 ..，不把带相对段的路径原样回传给前端"""
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        (sub / "c").mkdir()
        # 通过 ../ 段访问 parent 的 parent
        r = authed_client.get(
            "/api/v1/workspace/browse",
            params={"path": f"{sub}/../../a/b/../b"},
        )
        assert r.status_code == 200
        data = D(r)
        # current 必须已规范化为真实路径（不含 ..）
        assert ".." not in data["current"]
        assert os.path.realpath(data["current"]) == os.path.realpath(str(sub))
        assert "c" in data["dirs"]

    def test_browse_resolves_symlink(self, authed_client, tmp_path):
        """browse 应解析符号链接：current/parent 反映真实位置，非链接位置"""
        real = tmp_path / "real_dir"
        real.mkdir()
        (real / "child").mkdir()
        link = tmp_path / "link_dir"
        try:
            os.symlink(str(real), str(link))
        except (OSError, AttributeError):
            pytest.skip("当前环境不支持符号链接")
        r = authed_client.get("/api/v1/workspace/browse", params={"path": str(link)})
        assert r.status_code == 200
        data = D(r)
        # resolved symlink: current 为真实路径而非 link 路径
        assert data["current"] == os.path.realpath(str(real))
        assert "child" in data["dirs"]


class TestWhitelistEndpoints:
    def test_add_to_whitelist(self, authed_client, pm):
        r = authed_client.post("/api/v1/workspace/whitelist", json={"command": "npm"})
        assert r.status_code == 200
        assert "npm" in D(r)["whitelist"]

    def test_get_whitelist(self, authed_client, pm):
        pm.add_to_whitelist("git")
        r = authed_client.get("/api/v1/workspace/whitelist")
        assert r.status_code == 200
        assert "git" in D(r)["whitelist"]

    def test_remove_from_whitelist(self, authed_client, pm):
        pm.add_to_whitelist("npm")
        r = authed_client.delete("/api/v1/workspace/whitelist/npm")
        assert r.status_code == 200
        assert "npm" not in D(r)["whitelist"]

    def test_add_extracts_cmd_name(self, authed_client, pm):
        """传入完整命令行，应只提取命令名"""
        r = authed_client.post("/api/v1/workspace/whitelist", json={"command": "npm install axios"})
        assert r.status_code == 200
        wl = D(r)["whitelist"]
        assert "npm" in wl
        assert "install" not in wl
        assert "axios" not in wl


@pytest.fixture(autouse=True)
def _clean_cmd_scopes():
    """每个测试前清空命令确认决策记录（模块级全局字典跨测试保留）。"""
    _pending_cmd_decisions.clear()
    yield
    _pending_cmd_decisions.clear()


class TestConfirmCmdEndpoint:
    def test_deny_decision(self, authed_client, pm):
        register_cmd_decision_scope("req1", "")
        r = authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req1",
            "decision": "deny",
        })
        assert r.status_code == 200
        assert D(r)["decision"] == "deny"

    def test_allow_once(self, authed_client, pm):
        register_cmd_decision_scope("req2", "")
        r = authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req2",
            "decision": "allow_once",
        })
        assert r.status_code == 200

    def test_allow_with_whitelist(self, authed_client, pm):
        register_cmd_decision_scope("req3", "")
        r = authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req3",
            "decision": "allow",
            "add_to_whitelist": True,
            "command": "cargo",
        })
        assert r.status_code == 200
        assert "cargo" in pm.get_whitelist()

    def test_pending_decision_consumed(self, authed_client, pm):
        from web.routers.workspace import get_pending_cmd_decision
        register_cmd_decision_scope("req4", "")
        authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req4",
            "decision": "allow_once",
        })
        # 第一次查询返回决策
        assert get_pending_cmd_decision("req4") == "allow_once"
        # 第二次查询返回 None（已消费）
        assert get_pending_cmd_decision("req4") is None

    def test_unknown_request_rejected(self, authed_client, pm):
        """未登记 scope 的确认请求应被拒绝（防止伪造 request_id）。"""
        r = authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "ghost",
            "decision": "allow_once",
        })
        assert r.status_code == 200
        assert D(r)["status"] == "unknown_request"

    def test_session_mismatch_rejected(self, authed_client, pm):
        """非发起会话回传决策应返回 403，且决策不被记录。"""
        from web.routers.workspace import get_pending_cmd_decision
        register_cmd_decision_scope("req5", "session-A")
        r = authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req5",
            "decision": "allow",
            "session_id": "session-B",
        })
        assert r.status_code == 403
        assert get_pending_cmd_decision("req5") is None

    def test_session_match_accepted(self, authed_client, pm):
        """发起会话回传决策应被接受。"""
        from web.routers.workspace import get_pending_cmd_decision
        register_cmd_decision_scope("req6", "session-A")
        r = authed_client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req6",
            "decision": "allow_once",
            "session_id": "session-A",
        })
        assert r.status_code == 200
        assert get_pending_cmd_decision("req6") == "allow_once"


class TestAuditEndpoint:
    def test_get_audit_empty(self, authed_client, pm):
        r = authed_client.get("/api/v1/workspace/audit", params={"limit": 10})
        assert r.status_code == 200
        assert "entries" in D(r)

    def test_get_audit_with_entries(self, authed_client, pm):
        from security.permission_manager import AuditEntry
        pm.add_audit_entry(AuditEntry(
            timestamp="2026-07-25T10:00:00",
            action="read", target="/tmp/x", cwd="/tmp", allowed=True,
        ))
        r = authed_client.get("/api/v1/workspace/audit", params={"limit": 10})
        assert r.status_code == 200
        entries = D(r)["entries"]
        assert len(entries) == 1
        assert entries[0]["action"] == "read"
