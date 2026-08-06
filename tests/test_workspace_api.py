"""workspace API 路由集成测试

响应格式为 Envelope: {"ok": true, "data": {...}}
测试中用 D(r) 辅助函数解包 data 字段。
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import get_permission_manager
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
    return TestClient(app)


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


class TestWorkspaceEndpoints:
    def test_get_unauthorized(self, client, pm):
        pm.clear_cwd()
        r = client.get("/api/v1/workspace")
        assert r.status_code == 200
        data = D(r)
        assert data["authorized"] is False
        assert data["path"] == ""

    def test_confirm_authorization(self, client, pm, tmp_path):
        r = client.post("/api/v1/workspace/confirm", json={"path": str(tmp_path)})
        assert r.status_code == 200
        data = D(r)
        assert data["authorized"] is True
        assert data["path"] == str(tmp_path)
        assert "authorized_at" in data
        assert pm.is_cwd_authorized() is True

    def test_confirm_invalid_path(self, client, pm):
        r = client.post("/api/v1/workspace/confirm", json={"path": "/nonexistent/path/xyz"})
        assert r.status_code == 400

    def test_revoke(self, client, pm, tmp_path):
        client.post("/api/v1/workspace/confirm", json={"path": str(tmp_path)})
        r = client.delete("/api/v1/workspace")
        assert r.status_code == 200
        assert D(r)["authorized"] is False
        assert pm.is_cwd_authorized() is False

    def test_browse_directory(self, client, tmp_path):
        (tmp_path / "subdir1").mkdir()
        (tmp_path / "subdir2").mkdir()
        (tmp_path / "file.txt").write_text("hi")
        r = client.get("/api/v1/workspace/browse", params={"path": str(tmp_path)})
        assert r.status_code == 200
        data = D(r)
        assert "subdir1" in data["dirs"]
        assert "subdir2" in data["dirs"]
        assert "file.txt" not in data["dirs"]

    def test_browse_invalid_path(self, client):
        r = client.get("/api/v1/workspace/browse", params={"path": "/nonexistent/xyz"})
        assert r.status_code == 400

    def test_browse_returns_parent(self, client, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        r = client.get("/api/v1/workspace/browse", params={"path": str(sub)})
        assert r.status_code == 200
        assert D(r)["parent"] is not None


class TestWhitelistEndpoints:
    def test_add_to_whitelist(self, client, pm):
        r = client.post("/api/v1/workspace/whitelist", json={"command": "npm"})
        assert r.status_code == 200
        assert "npm" in D(r)["whitelist"]

    def test_get_whitelist(self, client, pm):
        pm.add_to_whitelist("git")
        r = client.get("/api/v1/workspace/whitelist")
        assert r.status_code == 200
        assert "git" in D(r)["whitelist"]

    def test_remove_from_whitelist(self, client, pm):
        pm.add_to_whitelist("npm")
        r = client.delete("/api/v1/workspace/whitelist/npm")
        assert r.status_code == 200
        assert "npm" not in D(r)["whitelist"]

    def test_add_extracts_cmd_name(self, client, pm):
        """传入完整命令行，应只提取命令名"""
        r = client.post("/api/v1/workspace/whitelist", json={"command": "npm install axios"})
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
    def test_deny_decision(self, client, pm):
        register_cmd_decision_scope("req1", "")
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req1",
            "decision": "deny",
        })
        assert r.status_code == 200
        assert D(r)["decision"] == "deny"

    def test_allow_once(self, client, pm):
        register_cmd_decision_scope("req2", "")
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req2",
            "decision": "allow_once",
        })
        assert r.status_code == 200

    def test_allow_with_whitelist(self, client, pm):
        register_cmd_decision_scope("req3", "")
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req3",
            "decision": "allow",
            "add_to_whitelist": True,
            "command": "cargo",
        })
        assert r.status_code == 200
        assert "cargo" in pm.get_whitelist()

    def test_pending_decision_consumed(self, client, pm):
        from web.routers.workspace import get_pending_cmd_decision
        register_cmd_decision_scope("req4", "")
        client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req4",
            "decision": "allow_once",
        })
        # 第一次查询返回决策
        assert get_pending_cmd_decision("req4") == "allow_once"
        # 第二次查询返回 None（已消费）
        assert get_pending_cmd_decision("req4") is None

    def test_unknown_request_rejected(self, client, pm):
        """未登记 scope 的确认请求应被拒绝（防止伪造 request_id）。"""
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "ghost",
            "decision": "allow_once",
        })
        assert r.status_code == 200
        assert D(r)["status"] == "unknown_request"

    def test_session_mismatch_rejected(self, client, pm):
        """非发起会话回传决策应返回 403，且决策不被记录。"""
        from web.routers.workspace import get_pending_cmd_decision
        register_cmd_decision_scope("req5", "session-A")
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req5",
            "decision": "allow",
            "session_id": "session-B",
        })
        assert r.status_code == 403
        assert get_pending_cmd_decision("req5") is None

    def test_session_match_accepted(self, client, pm):
        """发起会话回传决策应被接受。"""
        from web.routers.workspace import get_pending_cmd_decision
        register_cmd_decision_scope("req6", "session-A")
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req6",
            "decision": "allow_once",
            "session_id": "session-A",
        })
        assert r.status_code == 200
        assert get_pending_cmd_decision("req6") == "allow_once"


class TestAuditEndpoint:
    def test_get_audit_empty(self, client, pm):
        r = client.get("/api/v1/workspace/audit", params={"limit": 10})
        assert r.status_code == 200
        assert "entries" in D(r)

    def test_get_audit_with_entries(self, client, pm):
        from security.permission_manager import AuditEntry
        pm.add_audit_entry(AuditEntry(
            timestamp="2026-07-25T10:00:00",
            action="read", target="/tmp/x", cwd="/tmp", allowed=True,
        ))
        r = client.get("/api/v1/workspace/audit", params={"limit": 10})
        assert r.status_code == 200
        entries = D(r)["entries"]
        assert len(entries) == 1
        assert entries[0]["action"] == "read"
