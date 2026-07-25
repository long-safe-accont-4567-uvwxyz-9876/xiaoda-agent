"""ToolExecutor 工作目录边界拦截器测试"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import get_permission_manager
from tool_engine.tool_executor import ToolExecutor


@pytest.fixture
def executor():
    return ToolExecutor()


@pytest.fixture
def pm():
    """每个测试前清理全局 PermissionManager 状态"""
    pm = get_permission_manager()
    pm.clear_cwd()
    pm.set_whitelist([])
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
    async def test_needs_confirmation_returns_special_marker(self, executor, pm):
        """端到端：非白名单命令返回 needs_confirmation 标记"""
        pm.set_cwd("/tmp")
        pm.set_whitelist([])
        # cargo build 不匹配 sandbox 危险模式，也不在白名单
        result = await executor.execute("shell_command", {"command": "cargo build"})
        assert not result.success
        assert "__NEEDS_CONFIRMATION__" in (result.error or "")
