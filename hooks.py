"""小妲 AI Agent 钩子系统 (精简版)

保留 SecurityPreCheck + GateGuardHook 两类 PreToolUse 钩子，
用于安全预检和证据门禁。其余钩子类型保留枚举兼容但不再注册。
"""

import asyncio
import os
import re
import threading
from enum import Enum
from dataclasses import dataclass
from typing import Any
from loguru import logger

from core.risk_classifier import RiskClassifier, EvidenceGate, PostValidator, RiskLevel


# ── 钩子类型 ──────────────────────────────────────────────

class HookType(Enum):
    """钩子类型枚举 (仅 PRE_TOOL_USE 活跃, 其余保留兼容)"""
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"                 # 保留枚举, 不再注册
    POST_TOOL_USE_FAILURE = "post_tool_use_failure"  # 保留枚举
    USER_PROMPT_SUBMIT = "user_prompt_submit"        # 保留枚举
    SUBAGENT_START = "subagent_start"                # 保留枚举
    SUBAGENT_STOP = "subagent_stop"                  # 保留枚举
    PRE_COMPACT = "pre_compact"                      # 保留枚举
    POST_RESPONSE = "post_response"                  # 保留枚举


# ── 钩子结果 ──────────────────────────────────────────────

@dataclass
class HookResult:
    allowed: bool = True
    reason: str = ""
    modified_args: dict | None = None
    modified_output: str | None = None
    post_action: str | None = None
    additional_context: str | None = None
    updated_tool_output: Any | None = None
    decision: str | None = None


# ── 钩子基类 ──────────────────────────────────────────────

class BaseHook:
    """钩子基类，所有钩子继承此类"""
    name: str = ""
    hook_type: HookType = HookType.PRE_TOOL_USE
    tool_filter: set[str] | None = None
    matcher: str | None = None
    timeout: float = 60.0

    def matches_tool(self, tool_name: str) -> bool:
        if self.tool_filter is not None:
            return tool_name in self.tool_filter
        if self.matcher is not None:
            return bool(re.search(self.matcher, tool_name))
        return True

    async def execute(self, context: dict) -> HookResult:
        return HookResult()


# ── 钩子引擎 ──────────────────────────────────────────────

class HookEngine:
    """钩子引擎 (精简版) — 仅 PreToolUse 链活跃。"""

    def __init__(self) -> None:
        self._hooks: dict[HookType, list[BaseHook]] = {t: [] for t in HookType}

    def register(self, hook: BaseHook) -> None:
        """注册钩子"""
        self._hooks[hook.hook_type].append(hook)
        logger.debug("hooks.registered", name=hook.name, type=hook.hook_type.value)

    def reset_evidence_gate(self) -> None:
        """清空证据门禁的读取记录（请求间隔离）。"""
        for hook in self._hooks[HookType.PRE_TOOL_USE]:
            if isinstance(hook, GateGuardHook):
                hook._evidence_gate.clear()

    async def fire_pre_tool_use(self, tool_name: str, arguments: dict,
                                 user_input: str = "", safe_mode: bool = False) -> HookResult:
        """触发 PreToolUse 钩子链，任何钩子返回 allowed=False 则阻止执行"""
        merged_args = arguments
        for hook in self._hooks[HookType.PRE_TOOL_USE]:
            if not hook.matches_tool(tool_name):
                continue
            try:
                result = await asyncio.wait_for(
                    hook.execute({
                        "tool_name": tool_name,
                        "arguments": merged_args,
                        "user_input": user_input,
                        "safe_mode": safe_mode,
                    }),
                    timeout=hook.timeout,
                )
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning("hooks.pre_tool_use.timeout", hook=hook.name, timeout=hook.timeout)
                continue
            except Exception as e:
                logger.error("hooks.pre_tool_use.error", hook=hook.name, error=str(e))
                continue

            if not result.allowed:
                logger.warning("hooks.pre_tool_use.blocked",
                               hook=hook.name, tool=tool_name, reason=result.reason)
                return result

            if result.modified_args is not None:
                merged_args = result.modified_args

        return HookResult(allowed=True, modified_args=merged_args if merged_args is not arguments else None)

    # ── 以下方法保留兼容签名, 返回空结果 ──

    async def fire_post_tool_use(self, tool_name: str, arguments: dict,
                                  output: str, user_input: str = "") -> HookResult:
        """PostToolUse 钩子 (已精简, 返回空结果)"""
        return HookResult()

    async def fire_post_response(self) -> None:
        """PostResponse 钩子 (已精简, 空操作)"""
        pass

    async def fire_post_tool_use_failure(self, tool_name: str, arguments: dict,
                                          error: str, user_input: str = "") -> HookResult:
        """PostToolUseFailure 钩子 (已精简, 返回空结果)"""
        return HookResult()

    async def fire_user_prompt_submit(self, user_input: str, user_id: str = "") -> HookResult:
        """UserPromptSubmit 钩子 (已精简, 返回空结果)"""
        return HookResult()

    async def fire_subagent_start(self, agent_id: str, agent_type: str) -> HookResult:
        """SubagentStart 钩子 (已精简)"""
        return HookResult()

    async def fire_subagent_stop(self, agent_id: str, agent_type: str) -> HookResult:
        """SubagentStop 钩子 (已精简)"""
        return HookResult()

    async def fire_pre_compact(self, trigger: str = "auto", custom_instructions: str | None = None) -> HookResult:
        """PreCompact 钩子 (已精简)"""
        return HookResult()

    def get_registered_hooks(self) -> list[dict]:
        """获取已注册的钩子列表"""
        result = []
        for hook_type, hooks in self._hooks.items():
            for hook in hooks:
                result.append({
                    "name": hook.name,
                    "type": hook_type.value,
                    "tool_filter": list(hook.tool_filter) if hook.tool_filter else None,
                    "matcher": hook.matcher,
                    "timeout": hook.timeout,
                })
        return result


# ── 内置钩子 ──────────────────────────────────────────────

class SecurityPreCheck(BaseHook):
    """安全预检 - 调用 SecurityFilter 检查工具名称和参数中的安全威胁"""

    name = "security_pre_check"
    hook_type = HookType.PRE_TOOL_USE
    tool_filter = None
    matcher = r"shell_command|execute_code|python_executor|write_file|edit_file|create_file|agnes_image|agnes_video"

    def __init__(self) -> None:
        self._filter = None

    def _get_filter(self) -> Any:
        if self._filter is None:
            from security.security import SecurityFilter
            self._filter = SecurityFilter()
        return self._filter

    async def execute(self, context: dict) -> HookResult:
        tool_name = context.get("tool_name", "")
        arguments = context.get("arguments", {})
        user_input = context.get("user_input", "")

        sec_filter = self._get_filter()

        check_parts = [tool_name]
        if arguments:
            for v in arguments.values():
                if isinstance(v, str) and v:
                    check_parts.append(v)
        if user_input:
            check_parts.append(user_input)
        check_text = " ".join(check_parts)

        result = sec_filter.check_user_input(check_text)

        if result.action == "block":
            from security.permission_manager import get_permission_manager
            pm = get_permission_manager()
            if pm.is_dev_mode():
                logger.warning(
                    f"[DEV_MODE] SecurityPreCheck 降级为 warn: "
                    f"tool={tool_name}, threat={result.threat_type}, "
                    f"confidence={result.confidence:.2f}"
                )
                return HookResult(allowed=True)
            if pm.is_bypass_mode():
                return HookResult(allowed=True)
            logger.warning(
                f"SecurityPreCheck 阻断: tool={tool_name}, "
                f"threat={result.threat_type}, confidence={result.confidence:.2f}"
            )
            return HookResult(
                allowed=False,
                reason=f"安全预检拦截: 检测到{result.threat_type}威胁 (置信度={result.confidence:.2f})"
            )

        if result.action == "warn":
            logger.warning(
                f"SecurityPreCheck warn: tool={tool_name}, "
                f"threat={result.threat_type}, confidence={result.confidence:.2f}"
            )

        return HookResult(allowed=True)


class GateGuardHook(BaseHook):
    """质量门禁：危险分级 + 证据门禁 — 对修改性工具执行风险预检。"""

    name = "gate_guard"
    hook_type = HookType.PRE_TOOL_USE
    tool_filter = None

    def __init__(self) -> None:
        self._risk_classifier = RiskClassifier()
        self._evidence_gate = EvidenceGate()
        self._post_validator = PostValidator()

    async def execute(self, context: dict) -> HookResult:
        tool_name = context.get("tool_name", "")
        arguments = context.get("arguments", {})

        file_path = (
            arguments.get("file_path", "")
            or arguments.get("path", "")
            or arguments.get("filename", "")
        )

        if not file_path:
            risk = self._risk_classifier.classify(tool_name, arguments)
            if risk >= RiskLevel.FORBIDDEN:
                return HookResult(allowed=False, reason="危险操作，已拒绝")
            if risk >= RiskLevel.HIGH:
                from security.permission_manager import get_permission_manager
                pm = get_permission_manager()
                if pm.is_bypass_mode():
                    logger.warning(f"GateGuardHook.bypass: tool={tool_name}, risk=HIGH, mode={pm.mode.value}")
                else:
                    return HookResult(
                        allowed=False, reason="高风险操作，需要用户确认",
                        additional_context="需要用户确认后才能执行此高风险操作",
                    )
            return HookResult(allowed=True)

        has_read = self._evidence_gate.has_read(file_path) if file_path else False
        file_exists = bool(file_path) and os.path.exists(file_path)
        check_result = self._risk_classifier.pre_check(
            tool_name, arguments, has_read_target=has_read, file_exists=file_exists
        )

        if not check_result["allow"]:
            reason = check_result["reason"]
            from security.permission_manager import get_permission_manager
            pm = get_permission_manager()
            if pm.is_bypass_mode():
                logger.warning(
                    f"GateGuardHook.bypass: tool={tool_name}, reason={reason}, "
                    f"mode={pm.mode.value}"
                )
            elif check_result.get("need_confirm"):
                return HookResult(
                    allowed=False,
                    reason=reason,
                    additional_context="需要用户确认后才能执行此高风险操作",
                )
            else:
                return HookResult(allowed=False, reason=reason)

        if tool_name in ("read_file", "cat", "list_dir") and file_path:
            self._evidence_gate.mark_read(file_path)

        return HookResult(allowed=True)


# ── 便捷函数 ──────────────────────────────────────────────

_default_engine: HookEngine | None = None
_engine_lock = threading.Lock()


def get_hook_engine() -> HookEngine:
    """获取全局钩子引擎"""
    global _default_engine
    if _default_engine is None:
        with _engine_lock:
            if _default_engine is None:
                _default_engine = HookEngine()
                _register_builtin_hooks(_default_engine)
    return _default_engine


def _register_builtin_hooks(engine: HookEngine) -> None:
    """注册内置钩子 (仅 PreToolUse 类)"""
    engine.register(SecurityPreCheck())
    engine.register(GateGuardHook())
