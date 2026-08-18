"""P0-1: Approver 显式审批抽象 — 测试

测试 tool_engine/approver.py 的 ApprovalOutcome、Approver 协议、
SessionApprover 会话级审批器，以及 ToolExecutor 的 approver 集成。
"""
import asyncio
import pytest

from tool_engine.approver import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalDecision,
    DefaultApprover,
    SessionApprover,
    Approver,
)
from tool_engine.tool_executor import ToolExecutor
from tool_engine.tool_registry import ToolResult


class TestApprovalOutcome:
    """ApprovalOutcome 枚举测试"""

    def test_outcome_values(self):
        assert ApprovalOutcome.ONCE == "once"
        assert ApprovalOutcome.ALWAYS_TOOL == "always_tool"
        assert ApprovalOutcome.ALWAYS_COMMAND == "always_command"
        assert ApprovalOutcome.DENY == "deny"

    def test_outcome_is_string_enum(self):
        assert isinstance(ApprovalOutcome.ONCE, str)
        assert isinstance(ApprovalOutcome.DENY, str)


class TestApprovalRequest:
    """ApprovalRequest 数据类测试"""

    def test_default_values(self):
        req = ApprovalRequest(tool_name="web_search", arguments={"query": "test"})
        assert req.tool_name == "web_search"
        assert req.arguments == {"query": "test"}
        assert req.risk_level == "low"
        assert req.reason == ""
        assert req.user_id == ""

    def test_with_all_fields(self):
        req = ApprovalRequest(
            tool_name="shell_command",
            arguments={"command": "ls"},
            risk_level="high",
            reason="shell execution",
            user_id="user1",
            session_id="sess1",
        )
        assert req.risk_level == "high"
        assert req.user_id == "user1"


class TestDefaultApprover:
    """DefaultApprover 测试 — 不拦截任何调用"""

    @pytest.mark.asyncio
    async def test_always_returns_once(self):
        approver = DefaultApprover()
        req = ApprovalRequest(tool_name="any_tool", arguments={})
        decision = await approver.approve(req)
        assert decision.outcome == ApprovalOutcome.ONCE


class TestSessionApprover:
    """SessionApprover 会话级审批器测试"""

    @pytest.mark.asyncio
    async def test_tool_whitelist(self):
        """ALWAYS_TOOL 决策后续调用自动放行"""
        approver = SessionApprover()
        approver.allow_tool("shell_command")

        req = ApprovalRequest(tool_name="shell_command", arguments={"command": "ls"})
        decision = await approver.approve(req)
        assert decision.outcome == ApprovalOutcome.ALWAYS_TOOL

    @pytest.mark.asyncio
    async def test_command_whitelist(self):
        """ALWAYS_COMMAND 决策后续相同命令自动放行"""
        approver = SessionApprover()
        approver.allow_command("shell_command", "git status")

        req = ApprovalRequest(
            tool_name="shell_command",
            arguments={"command": "git status"},
        )
        decision = await approver.approve(req)
        assert decision.outcome == ApprovalOutcome.ALWAYS_COMMAND

    @pytest.mark.asyncio
    async def test_command_whitelist_scoped_to_tool(self):
        """命令白名单绑定工具名，工具 A 批准的命令不被工具 B 复用"""
        approver = SessionApprover()
        approver.allow_command("shell_command", "ls")

        # 不同工具用相同命令不应命中白名单
        req_other = ApprovalRequest(
            tool_name="python_executor",
            arguments={"code": "ls"},
        )
        decision = await approver.approve(req_other)
        # 应委托内层（DefaultApprover 返回 ONCE），而非命中 ALWAYS_COMMAND
        assert decision.outcome == ApprovalOutcome.ONCE

    @pytest.mark.asyncio
    async def test_delegates_to_inner_when_not_allowed(self):
        """未命中白名单时委托给内层审批器"""
        class DenyAllApprover:
            async def approve(self, request):
                return ApprovalDecision(
                    outcome=ApprovalOutcome.DENY,
                    reason="denied by inner",
                )

        approver = SessionApprover(inner=DenyAllApprover())
        req = ApprovalRequest(tool_name="shell_command", arguments={"command": "rm -rf /"})
        decision = await approver.approve(req)
        assert decision.outcome == ApprovalOutcome.DENY

    @pytest.mark.asyncio
    async def test_always_tool_propagates_to_whitelist(self):
        """内层返回 ALWAYS_TOOL 时自动加入会话白名单"""
        class AlwaysToolApprover:
            async def approve(self, request):
                return ApprovalDecision(
                    outcome=ApprovalOutcome.ALWAYS_TOOL,
                    reason="always allow",
                )

        approver = SessionApprover(inner=AlwaysToolApprover())
        req = ApprovalRequest(tool_name="write_file", arguments={"path": "/tmp/test"})
        decision1 = await approver.approve(req)
        assert decision1.outcome == ApprovalOutcome.ALWAYS_TOOL

        # 第二次调用应直接命中白名单
        decision2 = await approver.approve(req)
        assert decision2.outcome == ApprovalOutcome.ALWAYS_TOOL
        assert "already approved" in decision2.reason

    @pytest.mark.asyncio
    async def test_revoke_tool(self):
        """撤销工具白名单"""
        approver = SessionApprover()
        approver.allow_tool("read_file")
        assert approver.is_tool_allowed("read_file")
        approver.revoke_tool("read_file")
        assert not approver.is_tool_allowed("read_file")

    @pytest.mark.asyncio
    async def test_revoke_command(self):
        """撤销命令白名单"""
        approver = SessionApprover()
        approver.allow_command("shell_command", "ls -la")
        assert approver.is_command_allowed("shell_command", "ls -la")
        approver.revoke_command("shell_command", "ls -la")
        assert not approver.is_command_allowed("shell_command", "ls -la")

    @pytest.mark.asyncio
    async def test_empty_command_not_whitelisted(self):
        """空命令不加入白名单"""
        approver = SessionApprover()
        approver.allow_command("shell_command", "")
        assert not approver.is_command_allowed("shell_command", "")


class TestApproverProtocol:
    """Approver Protocol 鸭子类型测试"""

    def test_session_approver_is_approver(self):
        """SessionApprover 符合 Approver Protocol"""
        approver = SessionApprover()
        assert isinstance(approver, Approver)

    def test_default_approver_is_approver(self):
        """DefaultApprover 符合 Approver Protocol"""
        approver = DefaultApprover()
        assert isinstance(approver, Approver)


class TestToolExecutorApproverIntegration:
    """ToolExecutor 与 Approver 集成测试"""

    @pytest.mark.asyncio
    async def test_default_approver_no_blocking(self):
        """不传 approver 时 DefaultApprover 不阻塞任何操作"""
        executor = ToolExecutor()
        # 找一个已注册的只读工具
        from tool_engine.tool_registry import get_tool
        tool = get_tool("get_current_time")
        if tool is None:
            pytest.skip("get_current_time not registered")
        result = await executor.execute("get_current_time", {}, user_id="test")
        # 只读工具应该能正常执行
        assert result.success

    @pytest.mark.asyncio
    async def test_deny_approver_blocks_execution(self):
        """DENY 决策阻止工具执行"""
        class DenyAllApprover:
            async def approve(self, request):
                return ApprovalDecision(
                    outcome=ApprovalOutcome.DENY,
                    reason="blocked by test",
                )

        executor = ToolExecutor(approver=DenyAllApprover())
        from tool_engine.tool_registry import get_tool
        tool = get_tool("get_current_time")
        if tool is None:
            pytest.skip("get_current_time not registered")
        result = await executor.execute("get_current_time", {}, user_id="test")
        assert not result.success
        assert "拒绝" in result.error

    @pytest.mark.asyncio
    async def test_approver_error_defaults_to_once(self):
        """Approver 抛异常时默认放行"""
        class ErrorApprover:
            async def approve(self, request):
                raise RuntimeError("approver crashed")

        executor = ToolExecutor(approver=ErrorApprover())
        from tool_engine.tool_registry import get_tool
        tool = get_tool("get_current_time")
        if tool is None:
            pytest.skip("get_current_time not registered")
        result = await executor.execute("get_current_time", {}, user_id="test")
        # approver 崩溃不阻塞工具执行
        assert result.success

    def test_executor_has_approver_property(self):
        """ToolExecutor 暴露 approver 属性"""
        executor = ToolExecutor()
        assert executor.approver is not None
        assert isinstance(executor.approver, DefaultApprover)
