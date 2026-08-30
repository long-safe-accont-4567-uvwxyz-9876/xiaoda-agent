"""requires_confirmation 强制执行测试（审计修复 2026-08-29）。

修复前：ToolExecutor.execute 从不读取注册表的 requires_confirmation 字段，
forget/service_manage 等声明确认的工具可被模型直接执行。
修复后：requires_confirmation=True 时本次审批请求 risk_level 提升为执行器
最高档（high），任何 approver 都必须走审批；DefaultApprover 对 high risk
fail-closed 拒绝（不再静默放行）。
"""
import pytest

import security.permission_manager as pm_mod
import tool_engine.tool_registry as registry
from tool_engine.approver import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    DefaultApprover,
)
from tool_engine.tool_executor import ToolExecutor
from tool_engine.tool_registry import ToolPermission, register_tool

_STUB_SCHEMA = {"type": "object", "properties": {}, "required": []}


def _register_stub(name: str, *, requires_confirmation: bool) -> list[dict]:
    """注册一个 READ_ONLY 桩工具（故意低权限，证明风险提升来自 requires_confirmation）。"""
    calls: list[dict] = []

    def _stub() -> str:
        calls.append({})
        return "stub-ok"

    register_tool(
        name=name,
        description="测试桩工具（审计修复 2026-08-29 requires_confirmation 测试专用）",
        schema=_STUB_SCHEMA,
        permission=ToolPermission.READ_ONLY,
        category="test",
        max_frequency=0,
        requires_confirmation=requires_confirmation,
    )(_stub)
    return calls


@pytest.fixture
def _perm_default_mode():
    """隔离权限模式：DEFAULT 档 + 未授权工作目录（避免其他测试遗留状态干扰）。"""
    pm = pm_mod.get_permission_manager()
    pm.set_mode(pm_mod.PermissionMode.DEFAULT)
    pm.clear_cwd()
    pm.set_whitelist([])
    yield pm


@pytest.fixture
def stub_confirm_tool():
    """注册 requires_confirmation=True 桩工具，测试后清理注册表。"""
    name = "stub_confirm_tool_audit2"
    calls = _register_stub(name, requires_confirmation=True)
    yield name, calls
    registry._tools.pop(name, None)


@pytest.fixture
def stub_plain_tool():
    """注册同权限但 requires_confirmation=False 的对照桩工具。"""
    name = "stub_plain_tool_audit2"
    calls = _register_stub(name, requires_confirmation=False)
    yield name, calls
    registry._tools.pop(name, None)


class RecordingApprover:
    """记录审批请求并放行的审批器（模拟修复前的宽松 approver）。"""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(outcome=ApprovalOutcome.ONCE, reason="test-once")


@pytest.mark.asyncio
async def test_requires_confirmation_blocked_by_default_approver(stub_confirm_tool, _perm_default_mode):
    """requires_confirmation=True 在 DefaultApprover 下不再静默执行（fail-closed 拒绝）。"""
    name, calls = stub_confirm_tool
    executor = ToolExecutor()  # 不传 approver → DefaultApprover
    result = await executor.execute(name, {})
    assert not result.success
    assert "拒绝" in (result.error or "")
    assert calls == []  # 桩工具未被真正执行


@pytest.mark.asyncio
async def test_requires_confirmation_promotes_risk_level(stub_confirm_tool, _perm_default_mode):
    """requires_confirmation=True → 审批请求 risk_level 提升为 high。"""
    name, _ = stub_confirm_tool
    approver = RecordingApprover()
    executor = ToolExecutor(approver=approver)
    result = await executor.execute(name, {})
    assert result.success
    assert len(approver.requests) == 1
    assert approver.requests[0].risk_level == "high"


@pytest.mark.asyncio
async def test_plain_tool_keeps_low_risk(stub_plain_tool, _perm_default_mode):
    """对照组：同权限 READ_ONLY 但未声明确认的工具风险保持 low。"""
    name, _ = stub_plain_tool
    approver = RecordingApprover()
    executor = ToolExecutor(approver=approver)
    result = await executor.execute(name, {})
    assert result.success
    assert approver.requests[0].risk_level == "low"


@pytest.mark.asyncio
async def test_human_approval_approver_gates_confirmation_tool(stub_confirm_tool, _perm_default_mode):
    """requires_confirmation=True 桩工具经 HumanApprovalApprover 走 gate 审批（owner 白名单放行）。"""
    from security.human_approval import HumanApprovalApprover, HumanApprovalGate

    name, calls = stub_confirm_tool
    gate = HumanApprovalGate()
    gate.register_auto_approve_user("owner1")
    executor = ToolExecutor(approver=HumanApprovalApprover(gate, user_id="owner1"))
    result = await executor.execute(name, {})
    assert result.success
    assert calls == [{}]  # 白名单放行后桩工具真正执行


class TestDefaultApproverHighRisk:
    """DefaultApprover 高风险不自动放行（fail-closed）单元测试。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("risk", ["high", "critical", "HIGH"])
    async def test_high_risk_denied(self, risk):
        decision = await DefaultApprover().approve(ApprovalRequest(
            tool_name="any_tool", arguments={}, risk_level=risk))
        assert decision.outcome == ApprovalOutcome.DENY

    @pytest.mark.asyncio
    @pytest.mark.parametrize("risk", ["low", "medium", ""])
    async def test_low_medium_still_once(self, risk):
        decision = await DefaultApprover().approve(ApprovalRequest(
            tool_name="any_tool", arguments={}, risk_level=risk))
        assert decision.outcome == ApprovalOutcome.ONCE
