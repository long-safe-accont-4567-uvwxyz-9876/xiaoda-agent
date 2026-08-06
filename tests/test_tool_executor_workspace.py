"""ToolExecutor 工作目录边界拦截器测试"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import get_permission_manager
from tool_engine.tool_executor import ToolExecutor


@pytest.fixture(autouse=True)
def _ensure_tools_registered():
    """确保内置工具已注册（防御性 fixture）。

    TestExecuteIntegration 测试依赖 read_file/shell_command 等工具已注册。
    其他测试（如 test_smoke.py）可能调用 clear_tools() 清空全局工具注册表，
    若本测试在其后运行，get_tool 会返回 None 导致 execute 返回
    "还没有学会"错误而非工作目录边界错误。

    register_builtin_tools_lazy 是幂等的：已存在的工具不会被覆盖。
    """
    from tool_engine.tool_registry import register_builtin_tools_lazy
    register_builtin_tools_lazy()
    yield


@pytest.fixture
def executor():
    return ToolExecutor()


@pytest.fixture
def pm():
    """每个测试前清理全局 PermissionManager 状态"""
    pm = get_permission_manager()
    pm.clear_cwd()
    pm.set_whitelist([])
    # 清空审计环形缓冲：全局单例的 _audit_buffer 跨测试保留，
    # 本文件 test_delete_action_classified 写入的 delete 条目会污染
    # test_workspace_api::test_get_audit_with_entries 的 len 断言。
    pm.clear_audit_log()
    return pm


class TestWorkspaceBoundaryFile:
    def test_unauthorized_file_tool_rejected(self, executor, pm):
        """未授权时文件工具直接拒绝"""
        pm.clear_cwd()
        err = executor._enforce_workspace_boundary("read_file", {"path": "/tmp/any.txt"})
        assert err is not None
        assert "工作目录" in err

    def test_authorized_path_inside_cwd_passes(self, executor, pm, tmp_path):
        """已授权 + cwd 内路径 → 放行"""
        pm.set_cwd(str(tmp_path))
        err = executor._enforce_workspace_boundary("read_file", {"path": str(tmp_path / "x.txt")})
        assert err is None

    def test_authorized_path_outside_cwd_rejected(self, executor, pm, tmp_path):
        """已授权 + cwd 外路径 → 拒绝"""
        pm.set_cwd(str(tmp_path))
        err = executor._enforce_workspace_boundary("read_file", {"path": "/etc/passwd"})
        assert err is not None
        assert "工作目录" in err

    def test_write_file_checked(self, executor, pm, tmp_path):
        """write_file 也受约束"""
        pm.set_cwd(str(tmp_path))
        # cwd 内
        err = executor._enforce_workspace_boundary("write_file", {"path": str(tmp_path / "f.txt")})
        assert err is None
        # cwd 外
        err = executor._enforce_workspace_boundary("write_file", {"path": "/etc/outside.txt"})
        assert err is not None

    def test_non_file_tool_not_checked(self, executor, pm, tmp_path):
        """非文件/shell 工具不受 workspace 约束"""
        pm.set_cwd(str(tmp_path))
        err = executor._enforce_workspace_boundary("web_search", {"query": "test"})
        assert err is None


class TestWorkspaceBoundaryShell:
    def test_unauthorized_shell_rejected(self, executor, pm):
        """未授权时 shell 工具直接拒绝"""
        pm.clear_cwd()
        err = executor._enforce_workspace_boundary("shell_command", {"command": "echo hi"})
        assert err is not None
        assert "工作目录" in err

    def test_whitelisted_command_passes(self, executor, pm):
        """已授权 + 白名单命令 → 放行"""
        pm.set_cwd("/tmp")
        pm.add_to_whitelist("echo")
        err = executor._enforce_workspace_boundary("shell_command", {"command": "echo hi"})
        assert err is None

    def test_blacklisted_command_rejected(self, executor, pm):
        """已授权 + 黑名单命令 → 永远拒绝"""
        pm.set_cwd("/tmp")
        pm.set_whitelist(["rm"])  # 即使 rm 在白名单
        err = executor._enforce_workspace_boundary("shell_command", {"command": "rm -rf /"})
        assert err is not None
        assert "危险" in err or "拦截" in err

    def test_unknown_command_needs_confirmation(self, executor, pm):
        """已授权 + 非白名单非黑名单命令 → 返回 needs_confirmation 标记"""
        pm.set_cwd("/tmp")
        pm.set_whitelist([])
        # 用 cargo build（不匹配 sandbox 危险模式，也不在白名单）
        err = executor._enforce_workspace_boundary("shell_command", {"command": "cargo build"})
        assert err is not None
        assert err.startswith("__NEEDS_CONFIRMATION__:")

    def test_python_executor_checked(self, executor, pm):
        """python_executor 也受 shell 约束"""
        pm.set_cwd("/tmp")
        pm.set_whitelist([])
        err = executor._enforce_workspace_boundary("python_executor", {"code": "print(1)"})
        # python_executor 用 code 参数；命令名提取为 print，不在白名单
        assert err is not None


class TestExecuteIntegration:
    @pytest.mark.asyncio
    async def test_unauthorized_execute_returns_fail(self, executor, pm):
        """端到端：未授权时 execute 返回失败结果"""
        pm.clear_cwd()
        result = await executor.execute("read_file", {"path": "/tmp/any.txt"})
        assert not result.success
        assert "工作目录" in result.error

    @pytest.mark.asyncio
    async def test_needs_confirmation_deny_returns_user_decision(self, executor, pm):
        """端到端：非白名单命令需确认，用户拒绝 → 返回用户决策失败（非系统故障）"""
        pm.set_cwd("/tmp")
        pm.set_whitelist([])
        executor._decision_provider = lambda rid: "deny"
        # cargo build 不匹配 sandbox 危险模式，也不在白名单
        result = await executor.execute("shell_command", {"command": "cargo build"})
        assert not result.success
        assert result.user_decision is True
        assert "拒绝" in (result.error or "")


class TestWorkspaceAuditBuffer:
    """审计缓冲写入测试：边界检查决策应同步写入 PermissionManager._audit_buffer"""

    def test_file_blocked_writes_audit(self, executor, pm):
        """未授权文件工具 → 审计写入 allowed=False"""
        pm.clear_cwd()
        executor._enforce_workspace_boundary("read_file", {"path": "/tmp/any.txt"})
        entries = pm.get_audit_log(limit=10)
        assert len(entries) == 1
        assert entries[0]["allowed"] is False
        assert entries[0]["action"] == "read"
        assert "工作目录" in entries[0]["reason"]

    def test_file_allowed_writes_audit(self, executor, pm, tmp_path):
        """已授权 + cwd 内路径 → 审计写入 allowed=True"""
        pm.set_cwd(str(tmp_path))
        executor._enforce_workspace_boundary("write_file", {"path": str(tmp_path / "f.txt")})
        entries = pm.get_audit_log(limit=10)
        assert len(entries) == 1
        assert entries[0]["allowed"] is True
        assert entries[0]["action"] == "write"

    def test_shell_unauthorized_writes_audit(self, executor, pm):
        """未授权 shell → 审计写入 allowed=False, reason=未授权工作目录"""
        pm.clear_cwd()
        executor._enforce_workspace_boundary("shell_command", {"command": "echo hi"})
        entries = pm.get_audit_log(limit=10)
        assert len(entries) == 1
        assert entries[0]["allowed"] is False
        assert entries[0]["action"] == "exec"
        assert "未授权" in entries[0]["reason"]

    def test_shell_whitelisted_writes_audit(self, executor, pm):
        """已授权 + 白名单命令 → 审计写入 allowed=True"""
        pm.set_cwd("/tmp")
        pm.set_whitelist(["echo"])
        executor._enforce_workspace_boundary("shell_command", {"command": "echo hi"})
        entries = pm.get_audit_log(limit=10)
        assert len(entries) == 1
        assert entries[0]["allowed"] is True
        assert entries[0]["target"] == "echo hi"

    def test_shell_needs_confirmation_writes_audit(self, executor, pm):
        """非白名单命令 → 审计写入 allowed=False, reason=等待用户确认"""
        pm.set_cwd("/tmp")
        pm.set_whitelist([])
        executor._enforce_workspace_boundary("shell_command", {"command": "cargo build"})
        entries = pm.get_audit_log(limit=10)
        assert len(entries) == 1
        assert entries[0]["allowed"] is False
        assert "等待用户确认" in entries[0]["reason"]

    def test_delete_action_classified(self, executor, pm, tmp_path):
        """delete_file 工具的 action 应为 'delete'"""
        pm.set_cwd(str(tmp_path))
        # pm fixture 已通过 clear_audit_log() 清空缓冲，无需再访问私有字段
        executor._enforce_workspace_boundary("delete_file", {"path": str(tmp_path / "x.txt")})
        entries = pm.get_audit_log(limit=10)
        # I2 加强断言：验证隔离效果，确保只有本用例写入的 1 条 delete 审计
        assert len(entries) == 1, f"期望仅 1 条 delete 审计，实际 {len(entries)} 条"
        assert entries[0]["action"] == "delete"
