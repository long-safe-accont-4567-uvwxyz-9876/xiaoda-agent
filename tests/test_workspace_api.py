"""workspace API 路由集成测试

响应格式为 Envelope: {"ok": true, "data": {...}}
测试中用 D(r) 辅助函数解包 data 字段。
"""
import os
import sys
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import get_permission_manager
from web.routers.auth import get_current_user
from web.routers.workspace import router as workspace_router


def D(r):
    """解包 Envelope 响应的 data 字段"""
    return r.json()["data"]


# 测试专用: 绕过认证的假依赖
def _fake_authenticated_user():
    """测试专用：模拟已认证用户"""
    return "test-user@local"


@pytest.fixture
def app():
    """最小 FastAPI app，仅挂载 workspace 路由（避免全量 app 启动开销）

    功能测试覆盖：覆盖 dependency 以通过 auth，专注 workspace 业务逻辑
    安全测试（独立类 TestWorkspaceAuth）：不覆盖 dependency，验证 401
    """
    app = FastAPI()
    app.include_router(workspace_router, prefix="/api/v1")
    # 覆盖 get_current_user 让功能测试专注于 workspace 行为
    app.dependency_overrides[get_current_user] = _fake_authenticated_user
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

    def test_browse_invalid_path(self, client, tmp_path):
        """在允许的基础目录下, 不存在的路径返回 400（不被路径规则提前拦截）"""
        bad_path = os.path.join(str(tmp_path), "definitely_nonexistent_subdir_12345")
        r = client.get("/api/v1/workspace/browse", params={"path": bad_path})
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


class TestConfirmCmdEndpoint:
    def test_deny_decision(self, client, pm):
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req1",
            "decision": "deny",
        })
        assert r.status_code == 200
        assert D(r)["decision"] == "deny"

    def test_allow_once(self, client, pm):
        r = client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req2",
            "decision": "allow_once",
        })
        assert r.status_code == 200

    def test_allow_with_whitelist(self, client, pm):
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
        client.post("/api/v1/workspace/confirm_cmd", json={
            "request_id": "req4",
            "decision": "allow_once",
        })
        # 第一次查询返回决策
        assert get_pending_cmd_decision("req4") == "allow_once"
        # 第二次查询返回 None（已消费）
        assert get_pending_cmd_decision("req4") is None


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


# ============================================================
# 安全回归测试：认证与路径保护
# ============================================================

class TestWorkspaceAuthSecurity:
    """未认证访问所有 workspace 端点必须 401/403 —— 安全回归测试.

    Bug 场景: 原实现 workspace 路由完全无认证, 未授权用户可:
      1. GET  /workspace/browse?path=/etc  →  列出任意系统目录
      2. POST /workspace/confirm          →  授权任意路径为 workspace
      3. POST /workspace/whitelist        →  添加危险命令到白名单
      4. GET  /workspace/audit            →  读取操作审计日志
    修复: APIRouter 全局注入 Depends(get_current_user)
    """

    @pytest.fixture
    def no_auth_app(self):
        """不覆盖 dependency 的 app, 用于验证 auth 实际生效."""
        app = FastAPI()
        app.include_router(workspace_router, prefix="/api/v1")
        return app

    @pytest.fixture
    def no_auth_client(self, no_auth_app):
        return TestClient(no_auth_app)

    def test_get_workspace_requires_auth(self, no_auth_client):
        r = no_auth_client.get("/api/v1/workspace")
        # 未携带 Bearer token → 401 Unauthorized
        assert r.status_code in (401, 403)

    def test_confirm_workspace_requires_auth(self, no_auth_client):
        r = no_auth_client.post(
            "/api/v1/workspace/confirm",
            json={"path": "/tmp"},
        )
        assert r.status_code in (401, 403)

    def test_revoke_workspace_requires_auth(self, no_auth_client):
        r = no_auth_client.delete("/api/v1/workspace")
        assert r.status_code in (401, 403)

    def test_browse_directory_requires_auth(self, no_auth_client):
        r = no_auth_client.get(
            "/api/v1/workspace/browse",
            params={"path": "/tmp"},
        )
        assert r.status_code in (401, 403)

    def test_browse_etc_requires_auth(self, no_auth_client):
        """原 Bug: 直接 curl /workspace/browse?path=/etc 可列出 /etc"""
        r = no_auth_client.get(
            "/api/v1/workspace/browse",
            params={"path": "/etc"},
        )
        # 未认证 → 401/403; 即使有认证也应被路径规则拦截
        assert r.status_code in (401, 403)

    def test_get_whitelist_requires_auth(self, no_auth_client):
        r = no_auth_client.get("/api/v1/workspace/whitelist")
        assert r.status_code in (401, 403)

    def test_add_whitelist_requires_auth(self, no_auth_client):
        r = no_auth_client.post(
            "/api/v1/workspace/whitelist",
            json={"command": "rm"},
        )
        assert r.status_code in (401, 403)

    def test_remove_whitelist_requires_auth(self, no_auth_client):
        r = no_auth_client.delete("/api/v1/workspace/whitelist/ls")
        assert r.status_code in (401, 403)

    def test_confirm_cmd_requires_auth(self, no_auth_client):
        r = no_auth_client.post(
            "/api/v1/workspace/confirm_cmd",
            json={"request_id": "x", "decision": "allow"},
        )
        assert r.status_code in (401, 403)

    def test_audit_requires_auth(self, no_auth_client):
        r = no_auth_client.get("/api/v1/workspace/audit")
        assert r.status_code in (401, 403)


class TestWorkspaceBrowsePathSecurity:
    """即使认证通过, browse 端点也要阻止越权访问 —— 防御纵深测试."""

    def test_browse_sensitive_etc_blocked(self, client):
        """即使已认证, 也禁止浏览 /etc"""
        r = client.get("/api/v1/workspace/browse", params={"path": "/etc"})
        assert r.status_code == 403

    def test_browse_sensitive_etc_passwd_blocked(self, client):
        """即使已认证, 也禁止浏览 /etc 子目录"""
        r = client.get("/api/v1/workspace/browse", params={"path": "/etc/passwd"})
        # /etc/passwd 是文件 → 先被路径拦截 (403) 或被 is_dir 拦截 (400)
        assert r.status_code in (400, 403)

    def test_browse_root_blocked(self, client):
        """即使已认证, 也禁止浏览 /root"""
        r = client.get("/api/v1/workspace/browse", params={"path": "/root"})
        assert r.status_code == 403

    def test_browse_path_traversal_blocked(self, client, tmp_path):
        """通过 symlink 跳转 + ../../ 试图越到 /etc 的攻击必须被拦.

        验证 realpath 生效: 在允许目录 /tmp 下建 symlink → /etc,
        browse 请求通过 symlink 访问时应被 realpath 解析到 /etc → 403.
        纯 ../../ 攻击若仍落在允许基目录内则至少 4xx 阻止成功浏览.
        """
        # 攻击 1: symlink 跳转 /tmp/x → /etc
        attack_link = os.path.join(str(tmp_path), "attack_link")
        try:
            os.symlink("/etc", attack_link)
        except OSError:
            # 某些环境不允许 symlink, 退化到直接访问 /etc
            attack_link = "/etc"
        r = client.get("/api/v1/workspace/browse", params={"path": attack_link})
        # realpath 解析到 /etc → 403
        assert r.status_code == 403

        # 攻击 2: 纯 ../ 遍历（必须至少是 4xx, 绝对不能 200）
        bad2 = os.path.join(str(tmp_path), "..", "..", "..", "etc")
        r2 = client.get("/api/v1/workspace/browse", params={"path": bad2})
        assert r2.status_code >= 400, f"遍历攻击未拦截, status={r2.status_code}"

    def test_browse_outside_allowed_bases_blocked(self, client):
        """访问不在允许列表的路径必须 403（例如系统配置目录）"""
        r = client.get("/api/v1/workspace/browse", params={"path": "/usr/lib"})
        assert r.status_code == 403

    def test_browse_tmp_allowed(self, client):
        """/tmp 是允许的浏览目录（正常工作流使用）"""
        # 创建临时目录确保路径存在
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            r = client.get("/api/v1/workspace/browse", params={"path": tmp})
            assert r.status_code == 200
            data = D(r)
            assert data["current"] == os.path.realpath(tmp)
