"""P1-1: Permission Mode 五态扩展 — 测试

测试 security/permission_manager.py 新增的 DISCUSS/PLAN/INTERACTIVE/AUTO/CUSTOM 模式。
"""
import os
import pytest

from security.permission_manager import (
    PermissionMode,
    PermissionManager,
    READ_ONLY_MODES,
    AUTO_APPROVE_MODES,
    get_permission_manager,
)


class TestNewPermissionModes:
    """新增权限模式枚举测试"""

    def test_new_modes_exist(self):
        """新增的五种模式都存在"""
        assert PermissionMode.DISCUSS.value == "discuss"
        assert PermissionMode.PLAN.value == "plan"
        assert PermissionMode.INTERACTIVE.value == "interactive"
        assert PermissionMode.AUTO.value == "auto"
        assert PermissionMode.CUSTOM.value == "custom"

    def test_read_only_modes(self):
        """READ_ONLY_MODES 包含 DISCUSS 和 PLAN"""
        assert PermissionMode.DISCUSS in READ_ONLY_MODES
        assert PermissionMode.PLAN in READ_ONLY_MODES
        assert PermissionMode.INTERACTIVE not in READ_ONLY_MODES

    def test_auto_approve_modes(self):
        """AUTO_APPROVE_MODES 包含 AUTO/BYPASS/GOAT"""
        assert PermissionMode.AUTO in AUTO_APPROVE_MODES
        assert PermissionMode.BYPASS in AUTO_APPROVE_MODES
        assert PermissionMode.GOAT in AUTO_APPROVE_MODES


class TestPermissionManagerModes:
    """PermissionManager 新增模式行为测试"""

    @pytest.fixture
    def pm(self):
        """每个测试独立的 PermissionManager 实例"""
        return PermissionManager()

    def test_set_mode_discuss(self, pm):
        """切换到 DISCUSS 模式"""
        pm.set_mode(PermissionMode.DISCUSS)
        assert pm.mode == PermissionMode.DISCUSS

    def test_set_mode_plan(self, pm):
        """切换到 PLAN 模式"""
        pm.set_mode(PermissionMode.PLAN)
        assert pm.mode == PermissionMode.PLAN

    def test_set_mode_interactive(self, pm):
        """切换到 INTERACTIVE 模式"""
        pm.set_mode(PermissionMode.INTERACTIVE)
        assert pm.mode == PermissionMode.INTERACTIVE

    def test_set_mode_auto(self, pm):
        """切换到 AUTO 模式"""
        pm.set_mode(PermissionMode.AUTO)
        assert pm.mode == PermissionMode.AUTO

    def test_set_mode_custom(self, pm):
        """切换到 CUSTOM 模式"""
        pm.set_mode(PermissionMode.CUSTOM)
        assert pm.mode == PermissionMode.CUSTOM

    def test_set_mode_by_string(self, pm):
        """字符串设置模式"""
        pm.set_mode("discuss")
        assert pm.mode == PermissionMode.DISCUSS
        pm.set_mode("auto")
        assert pm.mode == PermissionMode.AUTO

    def test_set_mode_invalid_string(self, pm):
        """无效字符串回退到 DEFAULT"""
        pm.set_mode("invalid_mode")
        assert pm.mode == PermissionMode.DEFAULT


class TestCheckToolPermissionNewModes:
    """新增模式的工具权限检查测试"""

    @pytest.fixture
    def pm(self):
        return PermissionManager()

    def test_discuss_mode_allows_readonly(self, pm):
        """DISCUSS 模式允许只读工具"""
        pm.set_mode(PermissionMode.DISCUSS)
        # web_search 是只读工具
        allowed, reason = pm.check_tool_permission("web_search")
        assert allowed

    def test_discuss_mode_blocks_write(self, pm):
        """DISCUSS 模式阻止写操作"""
        pm.set_mode(PermissionMode.DISCUSS)
        # write_file 是写工具
        allowed, reason = pm.check_tool_permission("write_file")
        assert not allowed
        assert "只读" in reason

    def test_discuss_mode_blocks_execute(self, pm):
        """DISCUSS 模式阻止执行操作"""
        pm.set_mode(PermissionMode.DISCUSS)
        allowed, reason = pm.check_tool_permission("shell_command")
        assert not allowed
        assert "只读" in reason

    def test_plan_mode_allows_readonly(self, pm):
        """PLAN 模式允许只读工具"""
        pm.set_mode(PermissionMode.PLAN)
        allowed, reason = pm.check_tool_permission("web_search")
        assert allowed

    def test_plan_mode_blocks_write(self, pm):
        """PLAN 模式阻止写操作"""
        pm.set_mode(PermissionMode.PLAN)
        allowed, reason = pm.check_tool_permission("write_file")
        assert not allowed

    def test_auto_mode_allows_all(self, pm):
        """AUTO 模式全部放行"""
        pm.set_mode(PermissionMode.AUTO)
        allowed, _ = pm.check_tool_permission("web_search")
        assert allowed
        allowed, _ = pm.check_tool_permission("write_file")
        assert allowed
        allowed, _ = pm.check_tool_permission("shell_command")
        assert allowed

    def test_auto_mode_blocks_dangerous(self, pm):
        """AUTO 模式仍拦截危险命令"""
        pm.set_mode(PermissionMode.AUTO)
        allowed, reason = pm.check_tool_permission(
            "shell_command", {"command": "rm -rf /"})
        assert not allowed

    def test_custom_mode_auto_allow(self, pm):
        """CUSTOM 模式 auto_allow 工具直接放行"""
        pm.set_mode(PermissionMode.CUSTOM)
        pm.add_auto_allow_tool("write_file")
        allowed, reason = pm.check_tool_permission("write_file")
        assert allowed
        assert "auto-allowed" in reason

    def test_custom_mode_non_allow_sensitive(self, pm):
        """CUSTOM 模式非 auto_allow 的敏感工具需要确认"""
        pm.set_mode(PermissionMode.CUSTOM)
        allowed, reason = pm.check_tool_permission("shell_command")
        assert not allowed
        assert "确认" in reason

    def test_interactive_mode_readonly_pass(self, pm):
        """INTERACTIVE 模式只读工具放行"""
        pm.set_mode(PermissionMode.INTERACTIVE)
        allowed, _ = pm.check_tool_permission("web_search")
        assert allowed

    def test_interactive_mode_sensitive_needs_confirm(self, pm):
        """INTERACTIVE 模式敏感工具需要确认"""
        pm.set_mode(PermissionMode.INTERACTIVE)
        allowed, reason = pm.check_tool_permission("shell_command")
        assert not allowed
        assert "确认" in reason


class TestAutoAllowTools:
    """auto_allow 工具白名单管理测试"""

    @pytest.fixture
    def pm(self):
        return PermissionManager()

    def test_add_auto_allow(self, pm):
        pm.add_auto_allow_tool("write_file")
        assert "write_file" in pm.get_auto_allow_tools()

    def test_remove_auto_allow(self, pm):
        pm.add_auto_allow_tool("write_file")
        pm.remove_auto_allow_tool("write_file")
        assert "write_file" not in pm.get_auto_allow_tools()

    def test_set_auto_allow_tools(self, pm):
        pm.set_auto_allow_tools(["tool_a", "tool_b", "tool_c"])
        assert sorted(pm.get_auto_allow_tools()) == ["tool_a", "tool_b", "tool_c"]

    def test_get_auto_allow_tools_sorted(self, pm):
        pm.add_auto_allow_tool("zebra")
        pm.add_auto_allow_tool("apple")
        result = pm.get_auto_allow_tools()
        assert result == sorted(result)
