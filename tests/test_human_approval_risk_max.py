"""HumanApprovalApprover 审批风险取 max 测试（审计修复 2026-08-29）。

修复前：审批只按工具名查 HIGH_RISK_OPERATIONS 固定名单，忽略
request.risk_level，未知 mcp_*/插件名一律按低风险静默放行。
修复后：risk = max(按名风险, request.risk_level)，按严重度比较，
执行器判定的高风险不得被按名降级。
"""
import pytest

from security.human_approval import (
    ApprovalStatus,
    HumanApprovalApprover,
    HumanApprovalGate,
    RiskLevel,
    _coerce_risk_level,
)
from tool_engine.approver import ApprovalOutcome, ApprovalRequest


def _gate_auto_decide(status: ApprovalStatus = ApprovalStatus.APPROVED) -> HumanApprovalGate:
    """构造审批门禁：请求创建时立即由"测试审批人"作出决定。"""
    gate = HumanApprovalGate()

    def _decide(req):
        gate.decide(req.id, status, decided_by="tester", reason="test")

    gate.on_request(_decide)
    return gate


@pytest.mark.asyncio
async def test_executor_high_risk_not_downgraded_by_unknown_name():
    """执行器判定 high + 未知工具名：必须走 gate 审批，不得按名降级静默放行。"""
    gate = _gate_auto_decide()
    approver = HumanApprovalApprover(gate)
    decision = await approver.approve(ApprovalRequest(
        tool_name="mcp_unknown_danger", arguments={}, risk_level="high", user_id="u1"))
    assert decision.outcome == ApprovalOutcome.ONCE
    # 走了 gate 审批（metadata 带 request_id），而非 "low risk, auto-approved"
    assert decision.metadata.get("request_id")


@pytest.mark.asyncio
async def test_unknown_tool_low_risk_still_auto_approved():
    """未知工具名 + 低风险：保持自动放行（不改变低风险可用性）。"""
    gate = HumanApprovalGate()
    approver = HumanApprovalApprover(gate)
    decision = await approver.approve(ApprovalRequest(
        tool_name="mcp_unknown_readonly", arguments={}, risk_level="low"))
    assert decision.outcome == ApprovalOutcome.ONCE
    assert "auto-approved" in decision.reason
    assert gate.get_pending_requests() == []


@pytest.mark.asyncio
async def test_name_derived_high_not_downgraded_by_low_request():
    """按名高危（shell_command）+ request.risk_level=low：max 保持 HIGH 仍走审批。"""
    gate = _gate_auto_decide()
    approver = HumanApprovalApprover(gate)
    decision = await approver.approve(ApprovalRequest(
        tool_name="shell_command", arguments={"command": "ls"}, risk_level="low"))
    assert decision.outcome == ApprovalOutcome.ONCE
    assert decision.metadata.get("request_id")


@pytest.mark.asyncio
async def test_high_risk_timeout_fails_closed():
    """执行器判定 high + 无人决策：审批超时自动拒绝（fail-closed）。"""
    gate = HumanApprovalGate()  # 不注册任何决策者
    approver = HumanApprovalApprover(gate, default_timeout=0.2)
    decision = await approver.approve(ApprovalRequest(
        tool_name="mcp_unknown_exec", arguments={}, risk_level="high", user_id="u1"))
    assert decision.outcome == ApprovalOutcome.DENY


@pytest.mark.asyncio
async def test_owner_whitelist_still_auto_approves_high_risk():
    """owner 白名单对 max 后的高风险仍然自动通过（白名单语义不变）。"""
    gate = HumanApprovalGate()
    gate.register_auto_approve_user("owner1")
    approver = HumanApprovalApprover(gate, user_id="owner1")
    decision = await approver.approve(ApprovalRequest(
        tool_name="mcp_unknown_exec", arguments={}, risk_level="high"))
    assert decision.outcome == ApprovalOutcome.ALWAYS_TOOL
    assert decision.reason == "auto-approved (owner whitelist)"


def test_coerce_risk_level_mapping():
    """风险等级字符串（含大小写/枚举/未知值）到枚举的映射。"""
    assert _coerce_risk_level("low") == RiskLevel.LOW
    assert _coerce_risk_level("HIGH") == RiskLevel.HIGH
    assert _coerce_risk_level("critical") == RiskLevel.CRITICAL
    assert _coerce_risk_level(RiskLevel.MEDIUM) == RiskLevel.MEDIUM
    assert _coerce_risk_level("garbage") == RiskLevel.LOW
    assert _coerce_risk_level(None) == RiskLevel.LOW
    assert _coerce_risk_level("") == RiskLevel.LOW
