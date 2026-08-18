"""显式审批抽象 — 借鉴 OpenWorker coworker/permissions.py 的 Approver 回调设计。

将工具执行前的审批决策从硬编码逻辑抽象为可注入的回调协议，
支持四种审批决策：ONCE / ALWAYS_TOOL / ALWAYS_COMMAND / DENY。

设计要点：
- ``Approver`` 是一个 Protocol（鸭子类型），调用方可自定义实现
- ``ApprovalOutcome`` 枚举定义四种决策语义
- 不传 approver 时走原有逻辑（向后兼容）
- 与 security/human_approval.py 的 HumanApprovalGate 联动
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ApprovalOutcome(str, Enum):
    """审批决策结果枚举 — 借鉴 OpenWorker ApprovalOutcome 设计。

    语义：
    - ONCE: 一次性批准本次调用，下次仍需审批
    - ALWAYS_TOOL: 永久批准该工具（同会话内不再询问）
    - ALWAYS_COMMAND: 永久批准该具体命令（同会话内不再询问，仅对 shell 类工具有效）
    - DENY: 拒绝执行
    """
    ONCE = "once"
    ALWAYS_TOOL = "always_tool"
    ALWAYS_COMMAND = "always_command"
    DENY = "deny"


@dataclass
class ApprovalRequest:
    """审批请求上下文 — 传递给 Approver 回调的完整信息。"""
    tool_name: str
    arguments: dict[str, Any]
    risk_level: str = "low"          # low / medium / high / critical
    reason: str = ""                  # 为什么需要审批
    user_id: str = ""
    session_id: str = ""


@dataclass
class ApprovalDecision:
    """审批决策结果。"""
    outcome: ApprovalOutcome
    reason: str = ""
    # 批准时附带的元数据（如审批人、审批时间等）
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Approver(Protocol):
    """审批器协议 — 调用方实现此接口以控制工具执行的审批策略。

    实现示例：
        class MyApprover:
            async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
                if request.tool_name in self._allowed_tools:
                    return ApprovalDecision(ApprovalOutcome.ALWAYS_TOOL)
                # ... 走人工审批流程
                return ApprovalDecision(ApprovalOutcome.DENY, "用户拒绝")

    在 ToolExecutor.execute() 中注入：
        executor = ToolExecutor(db=db, approver=my_approver)
    """

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        """对工具调用请求做出审批决策。

        Args:
            request: 审批请求上下文（工具名、参数、风险等级等）

        Returns:
            ApprovalDecision — 包含决策结果和原因
        """
        ...


class DefaultApprover:
    """默认审批器 — 不拦截任何调用，兼容旧行为。

    当 ToolExecutor 未传入 approver 时使用此实现，
    所有调用直接返回 ONCE（放行但不记忆）。
    """

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            outcome=ApprovalOutcome.ONCE,
            reason="default: no approver configured",
        )


class SessionApprover:
    """会话级审批器 — 维护会话内的审批状态。

    - ALWAYS_TOOL 决策会将会话内的工具加入白名单
    - ALWAYS_COMMAND 决策会将会话内的具体命令加入白名单
    - 适合与 HumanApprovalGate 联动使用

    用法：
        approver = SessionApprover()
        # 外部审批流程返回 ALWAYS_TOOL 后
        approver.allow_tool("shell_command")
        # 后续 execute 检查时自动放行
    """

    def __init__(self, inner: Approver | None = None) -> None:
        """初始化会话审批器。

        Args:
            inner: 内层审批器，当会话白名单未命中时委托给内层决策。
                   为 None 时默认放行（兼容旧行为）。
        """
        self._inner = inner or DefaultApprover()
        self._allowed_tools: set[str] = set()
        # 命令白名单绑定 (tool_name, command) 对，防止跨工具授权泄漏
        # （工具 A 批准的命令不应被工具 B 复用）
        self._allowed_commands: set[tuple[str, str]] = set()

    def allow_tool(self, tool_name: str) -> None:
        """将会话内的工具加入白名单（对应 ALWAYS_TOOL 决策）。"""
        self._allowed_tools.add(tool_name)

    def allow_command(self, tool_name: str, command: str) -> None:
        """将会话内的具体命令加入白名单（对应 ALWAYS_COMMAND 决策）。

        绑定 tool_name + command 对，防止工具 A 批准的命令被工具 B 复用。
        """
        if command:
            self._allowed_commands.add((tool_name, command))

    def revoke_tool(self, tool_name: str) -> None:
        """撤销工具的会话级白名单。"""
        self._allowed_tools.discard(tool_name)

    def revoke_command(self, tool_name: str, command: str) -> None:
        """撤销命令的会话级白名单。"""
        self._allowed_commands.discard((tool_name, command))

    def is_tool_allowed(self, tool_name: str) -> bool:
        """检查工具是否在会话白名单中。"""
        return tool_name in self._allowed_tools

    def is_command_allowed(self, tool_name: str, command: str) -> bool:
        """检查命令是否在会话白名单中。"""
        return (tool_name, command) in self._allowed_commands

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        """审批逻辑：先查会话白名单，未命中则委托内层。"""
        # 检查工具级白名单
        if request.tool_name in self._allowed_tools:
            return ApprovalDecision(
                outcome=ApprovalOutcome.ALWAYS_TOOL,
                reason=f"tool {request.tool_name} already approved for session",
            )
        # 检查命令级白名单（绑定 tool_name，防止跨工具复用）
        command = request.arguments.get("command") or request.arguments.get("code") or ""
        if command and (request.tool_name, command) in self._allowed_commands:
            return ApprovalDecision(
                outcome=ApprovalOutcome.ALWAYS_COMMAND,
                reason="command already approved for session",
            )
        # 委托内层审批器
        decision = await self._inner.approve(request)
        # 根据内层决策更新会话白名单
        if decision.outcome == ApprovalOutcome.ALWAYS_TOOL:
            self.allow_tool(request.tool_name)
        elif decision.outcome == ApprovalOutcome.ALWAYS_COMMAND and command:
            self.allow_command(request.tool_name, command)
        return decision
