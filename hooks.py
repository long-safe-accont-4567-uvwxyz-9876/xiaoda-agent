"""小妲 AI Agent 钩子系统

借鉴 ECC (Everything Claude Code) 的 Hooks 系统，
实现 PreToolUse / PostToolUse / PostResponse 钩子机制，
用于安全预检、质量门禁、批量后处理等自动化。
"""

import asyncio
import re
import threading
from enum import Enum
from dataclasses import dataclass
from typing import Any
from loguru import logger

from core.risk_classifier import RiskClassifier, EvidenceGate, PostValidator, RiskLevel


# ── 钩子类型 ──────────────────────────────────────────────

class HookType(Enum):
    """钩子类型枚举，覆盖工具执行前后、用户提交、子代理启停等关键节点。"""
    PRE_TOOL_USE = "pre_tool_use"                   # 工具执行前
    POST_TOOL_USE = "post_tool_use"                 # 工具执行后
    POST_TOOL_USE_FAILURE = "post_tool_use_failure" # 工具执行失败
    USER_PROMPT_SUBMIT = "user_prompt_submit"       # 处理用户输入前
    SUBAGENT_START = "subagent_start"               # 子代理启动
    SUBAGENT_STOP = "subagent_stop"                 # 子代理停止
    PRE_COMPACT = "pre_compact"                     # 上下文压缩前
    POST_RESPONSE = "post_response"                 # 一次响应完成后（批量后处理）


# ── 钩子结果 ──────────────────────────────────────────────

@dataclass
class HookResult:
    allowed: bool = True                        # 是否允许继续（仅 PreToolUse 有效）
    reason: str = ""                            # 拒绝原因
    modified_args: dict | None = None           # 修改后的参数（PreToolUse 可修改参数）
    modified_output: str | None = None          # 修改后的输出（PostToolUse 可修改输出）
    post_action: str | None = None              # 后处理动作标识（PostToolUse 使用）
    additional_context: str | None = None       # 注入额外上下文到模型
    updated_tool_output: Any | None = None      # 完全替换工具输出
    decision: str | None = None                 # "block" 阻断，None 继续


# ── 钩子基类 ──────────────────────────────────────────────

class BaseHook:
    """钩子基类，所有钩子继承此类"""
    name: str = ""
    hook_type: HookType = HookType.PRE_TOOL_USE
    tool_filter: set[str] | None = None  # 仅对特定工具生效，None 表示所有工具
    matcher: str | None = None           # 正则匹配工具名称（如 "Bash|Edit"）
    timeout: float = 60.0                # 钩子执行超时（秒）

    def matches_tool(self, tool_name: str) -> bool:
        """检查此钩子是否应对给定工具名称触发"""
        if self.tool_filter is not None:
            return tool_name in self.tool_filter
        if self.matcher is not None:
            return bool(re.search(self.matcher, tool_name))
        return True  # 无过滤器表示匹配所有工具

    async def execute(self, context: dict) -> HookResult:
        """执行钩子逻辑

        context 包含：
        - tool_name: str - 工具名称
        - arguments: dict - 工具参数
        - output: str | None - 工具输出（仅 PostToolUse）
        - user_input: str - 用户原始输入
        - safe_mode: bool - 是否安全模式
        """
        return HookResult()


# ── 钩子引擎 ──────────────────────────────────────────────

class HookEngine:
    """钩子引擎，负责注册并触发各类钩子链。"""

    def __init__(self) -> None:
        self._hooks: dict[HookType, list[BaseHook]] = {t: [] for t in HookType}
        self._pending_post_actions: list[str] = []
        self._failure_trigger = None  # 失败触发器，由 AgentCore 注入

    def register(self, hook: BaseHook) -> None:
        """注册钩子"""
        self._hooks[hook.hook_type].append(hook)
        logger.debug("hooks.registered", name=hook.name, type=hook.hook_type.value)

    def reset_evidence_gate(self) -> None:
        """清空证据门禁的读取记录（请求间隔离）。

        EvidenceGate 是全局单例，_read_targets 会在请求间累积。
        必须在每个请求开始时调用此方法清空，避免跨请求状态泄漏。
        """
        for hook in self._hooks[HookType.PRE_TOOL_USE]:
            if isinstance(hook, GateGuardHook):
                hook._evidence_gate.clear()
                break

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
            except TimeoutError:
                logger.warning("hooks.pre_tool_use.timeout", hook=hook.name, timeout=hook.timeout)
                continue
            except Exception as e:
                logger.error("hooks.pre_tool_use.error", hook=hook.name, error=str(e))
                continue

            if not result.allowed:
                logger.warning("hooks.pre_tool_use.blocked",
                               hook=hook.name, tool=tool_name, reason=result.reason)
                return result

            # 参数修改链式传递
            if result.modified_args is not None:
                merged_args = result.modified_args

        return HookResult(allowed=True, modified_args=merged_args if merged_args is not arguments else None)

    async def fire_post_tool_use(self, tool_name: str, arguments: dict,
                                  output: str, user_input: str = "") -> HookResult:
        """触发 PostToolUse 钩子链"""
        current_output = output
        final_result = HookResult()
        for hook in self._hooks[HookType.POST_TOOL_USE]:
            if not hook.matches_tool(tool_name):
                continue
            try:
                result = await asyncio.wait_for(
                    hook.execute({
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "output": current_output,
                        "user_input": user_input,
                    }),
                    timeout=hook.timeout,
                )
            except TimeoutError:
                logger.warning("hooks.post_tool_use.timeout", hook=hook.name)
                continue
            except Exception as e:
                logger.error("hooks.post_tool_use.error", hook=hook.name, error=str(e))
                continue

            # 输出修改链式传递
            if result.modified_output is not None:
                current_output = result.modified_output
                final_result.modified_output = current_output

            # 累积后处理动作
            if result.post_action is not None:
                self._pending_post_actions.append(result.post_action)

        return final_result

    async def fire_post_response(self) -> None:
        """触发 PostResponse 钩子，执行所有累积的后处理动作"""
        for hook in self._hooks[HookType.POST_RESPONSE]:
            try:
                await hook.execute({
                    "pending_actions": list(self._pending_post_actions),
                })
            except Exception as e:
                logger.error("hooks.post_response.error", hook=hook.name, error=str(e))

        if self._pending_post_actions:
            logger.debug("hooks.post_response.flushed", count=len(self._pending_post_actions))
            self._pending_post_actions.clear()

    async def fire_post_tool_use_failure(self, tool_name: str, arguments: dict,
                                          error: str, user_input: str = "") -> HookResult:
        """触发 PostToolUseFailure 钩子，当工具执行失败时"""
        final_result = HookResult()
        for hook in self._hooks[HookType.POST_TOOL_USE_FAILURE]:
            if not hook.matches_tool(tool_name):
                continue
            try:
                result = await asyncio.wait_for(
                    hook.execute({
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "error": error,
                        "user_input": user_input,
                    }),
                    timeout=hook.timeout,
                )
                if result.additional_context:
                    final_result.additional_context = (final_result.additional_context or "") + result.additional_context + "\n"
            except TimeoutError:
                logger.warning("hooks.post_tool_use_failure.timeout", hook=hook.name)
                continue
            except Exception as e:
                logger.error("hooks.post_tool_use_failure.error", hook=hook.name, error=str(e))
                continue

        # 接入失败触发器（失败→反思→重试→经验归档）
        if self._failure_trigger:
            try:
                from core.failure_trigger import FailureContext
                _task = arguments.get("task", tool_name) if isinstance(arguments, dict) else tool_name
                # 从错误信息推断错误类型，供反思策略使用
                _error_lower = error.lower() if error else ""
                if "timeout" in _error_lower or "timed out" in _error_lower:
                    _error_type = "timeout"
                elif "auth" in _error_lower or "permission" in _error_lower or "unauthorized" in _error_lower:
                    _error_type = "auth_error"
                elif "not found" in _error_lower or "404" in _error_lower:
                    _error_type = "not found"
                else:
                    _error_type = "tool_error"
                context = FailureContext(
                    task=_task,
                    tool_name=tool_name,
                    error=error,
                    error_type=_error_type,
                    attempted_steps=[],
                )
                ft_result = await self._failure_trigger.on_failure(context)
                if ft_result.get("action") == "retry":
                    retry_hint = f"[失败触发器建议重试] 调整: {ft_result.get('adjustment', '')}"
                    final_result.additional_context = (final_result.additional_context or "") + retry_hint + "\n"
                elif ft_result.get("action") == "alternative":
                    alt_hint = f"[失败触发器建议替代方案] 方法: {ft_result.get('method', '')}"
                    final_result.additional_context = (final_result.additional_context or "") + alt_hint + "\n"
                elif ft_result.get("action") == "report":
                    report_hint = f"[失败触发器报告] 原因: {ft_result.get('reason', '')}"
                    final_result.additional_context = (final_result.additional_context or "") + report_hint + "\n"
            except Exception as e:
                logger.warning("hooks.failure_trigger.error", error=str(e))

        return final_result

    async def fire_user_prompt_submit(self, user_input: str, user_id: str = "") -> HookResult:
        """触发 UserPromptSubmit 钩子，在处理用户输入前"""
        final_result = HookResult()
        for hook in self._hooks[HookType.USER_PROMPT_SUBMIT]:
            try:
                result = await asyncio.wait_for(
                    hook.execute({
                        "user_input": user_input,
                        "user_id": user_id,
                    }),
                    timeout=hook.timeout,
                )
                if result.decision == "block":
                    logger.warning("hooks.user_prompt_submit.blocked", hook=hook.name)
                    return result
                if result.additional_context:
                    final_result.additional_context = (final_result.additional_context or "") + result.additional_context + "\n"
            except TimeoutError:
                logger.warning("hooks.user_prompt_submit.timeout", hook=hook.name)
                continue
            except Exception as e:
                logger.error("hooks.user_prompt_submit.error", hook=hook.name, error=str(e))
                continue
        return final_result

    async def fire_subagent_start(self, agent_id: str, agent_type: str) -> HookResult:
        """触发 SubagentStart 钩子，当子代理启动时"""
        final_result = HookResult()
        for hook in self._hooks[HookType.SUBAGENT_START]:
            try:
                result = await asyncio.wait_for(
                    hook.execute({
                        "agent_id": agent_id,
                        "agent_type": agent_type,
                    }),
                    timeout=hook.timeout,
                )
                if result.additional_context:
                    final_result.additional_context = (final_result.additional_context or "") + result.additional_context + "\n"
            except TimeoutError:
                logger.warning("hooks.subagent_start.timeout", hook=hook.name)
                continue
            except Exception as e:
                logger.error("hooks.subagent_start.error", hook=hook.name, error=str(e))
                continue
        return final_result

    async def fire_subagent_stop(self, agent_id: str, agent_type: str) -> HookResult:
        """触发 SubagentStop 钩子，当子代理停止时"""
        final_result = HookResult()
        for hook in self._hooks[HookType.SUBAGENT_STOP]:
            try:
                _result = await asyncio.wait_for(
                    hook.execute({
                        "agent_id": agent_id,
                        "agent_type": agent_type,
                    }),
                    timeout=hook.timeout,
                )
            except TimeoutError:
                logger.warning("hooks.subagent_stop.timeout", hook=hook.name)
                continue
            except Exception as e:
                logger.error("hooks.subagent_stop.error", hook=hook.name, error=str(e))
                continue
        return final_result

    async def fire_pre_compact(self, trigger: str = "auto", custom_instructions: str | None = None) -> HookResult:
        """触发 PreCompact 钩子，在上下文压缩前"""
        final_result = HookResult()
        for hook in self._hooks[HookType.PRE_COMPACT]:
            try:
                result = await asyncio.wait_for(
                    hook.execute({
                        "trigger": trigger,
                        "custom_instructions": custom_instructions,
                    }),
                    timeout=hook.timeout,
                )
                if result.additional_context:
                    final_result.additional_context = (final_result.additional_context or "") + result.additional_context + "\n"
            except TimeoutError:
                logger.warning("hooks.pre_compact.timeout", hook=hook.name)
                continue
            except Exception as e:
                logger.error("hooks.pre_compact.error", hook=hook.name, error=str(e))
                continue
        return final_result

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
        """延迟导入 SecurityFilter，避免循环依赖"""
        if self._filter is None:
            from security.security import SecurityFilter
            self._filter = SecurityFilter()
        return self._filter

    async def execute(self, context: dict) -> HookResult:
        tool_name = context.get("tool_name", "")
        arguments = context.get("arguments", {})
        user_input = context.get("user_input", "")

        sec_filter = self._get_filter()

        # 组合检查文本：工具名称 + 参数值 + 用户输入
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
                # 开发板模式：降级为 warn，不阻断
                logger.warning(
                    f"[DEV_MODE] SecurityPreCheck 降级为 warn: "
                    f"tool={tool_name}, threat={result.threat_type}, "
                    f"confidence={result.confidence:.2f}"
                )
                return HookResult(allowed=True)
            if pm.is_bypass_mode():
                # 绕过模式：直接允许
                return HookResult(allowed=True)
            # 生产/严格模式：阻断
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
    tool_filter = None  # 匹配所有工具，以便追踪读取操作并执行证据门禁

    def __init__(self) -> None:
        self._risk_classifier = RiskClassifier()
        self._evidence_gate = EvidenceGate()
        self._post_validator = PostValidator()

    async def execute(self, context: dict) -> HookResult:
        tool_name = context.get("tool_name", "")
        arguments = context.get("arguments", {})

        # 提取目标路径
        file_path = (
            arguments.get("file_path", "")
            or arguments.get("path", "")
            or arguments.get("filename", "")
        )

        # 证据门禁：检查是否已读取目标
        has_read = self._evidence_gate.has_read(file_path) if file_path else False
        # create_file: 检查目标文件是否已存在（创建新文件时豁免证据门禁）
        import os as _os
        file_exists = bool(file_path) and _os.path.exists(file_path)
        check_result = self._risk_classifier.pre_check(
            tool_name, arguments, has_read_target=has_read, file_exists=file_exists
        )

        if not check_result["allow"]:
            reason = check_result["reason"]
            # 检查权限模式：bypass/goat 模式下放行高风险操作（仅记录警告）
            from security.permission_manager import get_permission_manager
            pm = get_permission_manager()
            if pm.is_bypass_mode():
                logger.warning(
                    f"GateGuardHook.bypass: tool={tool_name}, reason={reason}, "
                    f"mode={pm.mode.value}"
                )
                # 继续执行，不阻断
            elif check_result.get("need_confirm"):
                return HookResult(
                    allowed=False,
                    reason=reason,
                    additional_context="需要用户确认后才能执行此高风险操作",
                )
            else:
                return HookResult(allowed=False, reason=reason)

        # 如果是读取操作，标记已读取（用于后续证据门禁）
        if tool_name in ("read_file", "cat", "list_dir") and file_path:
            self._evidence_gate.mark_read(file_path)

        return HookResult(allowed=True)


class PostValidateHook(BaseHook):
    """改完验证：L2+ 操作执行后自动验证结果完整性。"""

    name = "post_validate"
    hook_type = HookType.POST_TOOL_USE
    tool_filter = None  # 所有工具，内部按风险等级过滤

    def __init__(self) -> None:
        self._risk_classifier = RiskClassifier()
        self._post_validator = PostValidator()

    async def execute(self, context: dict) -> HookResult:
        tool_name = context.get("tool_name", "")
        arguments = context.get("arguments", {})
        output = context.get("output", "")

        risk = self._risk_classifier.classify(tool_name, arguments)
        if risk >= RiskLevel.MEDIUM:
            file_path = (
                arguments.get("file_path", "")
                or arguments.get("path", "")
                or arguments.get("filename", "")
            )
            result = {"output": output, "file_path": file_path}
            validation = self._post_validator.validate(tool_name, result, risk)
            if not validation["valid"]:
                logger.warning(
                    "post_validate.failed",
                    tool=tool_name,
                    reason=validation["reason"],
                )
                return HookResult(
                    reason=validation["reason"],
                    additional_context=validation["reason"],
                )

        return HookResult()


class OutputCompressionHook(BaseHook):
    """输出压缩：对超长工具输出截断并添加省略标记

    记忆相关工具（recall/remember）不截断，避免丢失关键上下文。
    """

    name = "output_compression"
    hook_type = HookType.POST_TOOL_USE
    tool_filter = None  # 所有工具

    MAX_LENGTH = 8000
    # 记忆相关工具不做截断，其输出对模型回复至关重要
    _EXEMPT_TOOLS = frozenset({
        "recall", "remember", "memory_search", "memory_recall",
        "recall_memory", "search_memory", "vector_search",
    })

    async def execute(self, context: dict) -> HookResult:
        output = context.get("output", "")
        if not output or len(output) <= self.MAX_LENGTH:
            return HookResult()

        tool_name = context.get("tool_name", "")
        if tool_name in self._EXEMPT_TOOLS:
            logger.debug("hooks.output_compression.skipped_exempt_tool",
                         tool=tool_name, output_len=len(output))
            return HookResult()

        truncated = output[:self.MAX_LENGTH]
        omitted = len(output) - self.MAX_LENGTH
        compressed = f"{truncated}\n\n... [已省略 {omitted} 字符] ..."
        logger.debug("hooks.output_compression.truncated",
                     original_len=len(output), omitted=omitted)
        return HookResult(modified_output=compressed)


class AuditLogHook(BaseHook):
    """审计日志：记录工具调用，累积到 PostResponse 时批量处理"""

    name = "audit_log"
    hook_type = HookType.POST_TOOL_USE
    tool_filter = None  # 所有工具

    async def execute(self, context: dict) -> HookResult:
        tool_name = context.get("tool_name", "")
        arguments = context.get("arguments", {})

        # 过滤敏感参数
        safe_args = self._filter_sensitive(arguments)
        action = f"tool_call:{tool_name}|args:{safe_args}"
        logger.debug("hooks.audit_log.recorded", tool=tool_name)
        return HookResult(post_action=action)

    @staticmethod
    def _filter_sensitive(args: dict) -> str:
        sensitive_keys = {'key', 'token', 'password', 'secret', 'api_key', 'credential'}
        filtered = {}
        for k, v in args.items():
            if any(s in k.lower() for s in sensitive_keys):
                filtered[k] = '***REDACTED***'
            else:
                filtered[k] = v
        # 截断避免过长
        text = str(filtered)
        if len(text) > 500:
            text = text[:500] + "..."
        return text


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
    """注册内置钩子"""
    engine.register(SecurityPreCheck())
    engine.register(GateGuardHook())
    engine.register(PostValidateHook())
    engine.register(OutputCompressionHook())
    engine.register(AuditLogHook())
