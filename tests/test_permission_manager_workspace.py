"""PermissionManager 工作目录授权扩展单元测试"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import PermissionManager, AuditEntry


@pytest.fixture
def pm():
    """每个测试用例独立的 PermissionManager 实例（避免全局单例污染）"""
    return PermissionManager()


@pytest.fixture
def tmp_cwd(tmp_path):
    return str(tmp_path)


class TestCwdAuthorization:
    def test_unauthorized_by_default(self, pm):
        assert pm.is_cwd_authorized() is False

    def test_set_cwd_authorizes(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        assert pm.is_cwd_authorized() is True
        assert pm._cwd == os.path.realpath(tmp_cwd)

    def test_clear_cwd_revokes(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        pm.clear_cwd()
        assert pm.is_cwd_authorized() is False
        assert pm._cwd == ""


class TestPathAllowed:
    def test_path_inside_cwd_allowed(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        target = os.path.join(tmp_cwd, "sub", "file.txt")
        allowed, _ = pm.is_path_allowed(target)
        assert allowed is True

    def test_path_outside_cwd_rejected(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        target = os.path.join(os.path.dirname(tmp_cwd), "outside.txt")
        allowed, reason = pm.is_path_allowed(target)
        assert allowed is False
        assert "工作目录" in reason

    def test_dotdot_escape_rejected(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        target = os.path.join(tmp_cwd, "..", "outside.txt")
        allowed, _ = pm.is_path_allowed(target)
        assert allowed is False

    def test_unauthorized_rejects_all(self, pm, tmp_cwd):
        allowed, _ = pm.is_path_allowed(tmp_cwd)
        assert allowed is False

    def test_empty_path_rejected(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        allowed, _ = pm.is_path_allowed("")
        assert allowed is False

    def test_cwd_itself_allowed(self, pm, tmp_cwd):
        pm.set_cwd(tmp_cwd)
        allowed, _ = pm.is_path_allowed(tmp_cwd)
        assert allowed is True


class TestCommandAllowed:
    def test_whitelisted_command_allowed(self, pm):
        pm.add_to_whitelist("npm")
        allowed, _, needs_conf = pm.is_command_allowed("npm install axios")
        assert allowed is True
        assert needs_conf is False

    def test_blacklisted_command_rejected(self, pm):
        allowed, reason, needs_conf = pm.is_command_allowed("rm -rf /")
        assert allowed is False
        assert needs_conf is False
        assert "危险" in reason or "拦截" in reason

    def test_unknown_command_needs_confirmation(self, pm):
        # 用一个既不在白名单也不命中黑名单的命令
        allowed, _, needs_conf = pm.is_command_allowed("wget http://example.com/x")
        assert allowed is False
        assert needs_conf is True

    def test_compound_command_blacklist_rejects_all(self, pm):
        pm.add_to_whitelist("git")
        allowed, _, _ = pm.is_command_allowed("git status; rm -rf /")
        assert allowed is False

    def test_compound_command_mixed_needs_confirmation(self, pm):
        pm.add_to_whitelist("git")
        allowed, _, needs_conf = pm.is_command_allowed("git status && npm install")
        assert allowed is False
        assert needs_conf is True

    def test_compound_all_whitelisted_passes(self, pm):
        pm.add_to_whitelist("git")
        pm.add_to_whitelist("npm")
        allowed, _, needs_conf = pm.is_command_allowed("git status && npm install")
        assert allowed is True
        assert needs_conf is False

    def test_empty_command_rejected(self, pm):
        allowed, _, needs_conf = pm.is_command_allowed("")
        assert allowed is False
        assert needs_conf is False


class TestWhitelistManagement:
    def test_add_and_get(self, pm):
        pm.add_to_whitelist("npm")
        assert "npm" in pm.get_whitelist()

    def test_remove(self, pm):
        pm.add_to_whitelist("npm")
        pm.remove_from_whitelist("npm")
        assert "npm" not in pm.get_whitelist()

    def test_set_whitelist(self, pm):
        pm.set_whitelist(["git", "npm", "python"])
        assert set(pm.get_whitelist()) == {"git", "npm", "python"}

    def test_add_extracts_cmd_name(self, pm):
        # 传入完整命令行，应只提取命令名
        pm.add_to_whitelist("npm install")
        assert "npm" in pm.get_whitelist()
        assert "install" not in pm.get_whitelist()


class TestAuditLog:
    def test_add_and_retrieve(self, pm):
        entry = AuditEntry(
            timestamp="2026-07-25T10:00:00",
            action="read",
            target="/tmp/file.txt",
            cwd="/tmp",
            allowed=True,
        )
        pm.add_audit_entry(entry)
        logs = pm.get_audit_log(limit=10)
        assert len(logs) == 1
        assert logs[0]["action"] == "read"
        assert logs[0]["allowed"] is True

    def test_limit_respected(self, pm):
        for i in range(5):
            pm.add_audit_entry(AuditEntry(
                timestamp=f"2026-07-25T10:00:0{i}",
                action="read", target=f"/tmp/f{i}", cwd="/tmp", allowed=True,
            ))
        logs = pm.get_audit_log(limit=3)
        assert len(logs) == 3

    def test_buffer_circular(self, pm):
        # 默认 maxlen=200，验证超限后自动淘汰
        for i in range(205):
            pm.add_audit_entry(AuditEntry(
                timestamp=f"2026-07-25T10:00:{i:03d}",
                action="read", target=f"/tmp/f{i}", cwd="/tmp", allowed=True,
            ))
        logs = pm.get_audit_log(limit=300)
        assert len(logs) == 200  # 被 maxlen 截断
