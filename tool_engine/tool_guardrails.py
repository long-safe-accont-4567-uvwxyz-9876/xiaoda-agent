"""工具调用护栏 - 检测和阻止工具调用循环"""
import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class ToolCallRecord:
    """工具调用记录"""
    tool_name: str
    arguments_hash: str  # 参数的简化哈希
    success: bool
    timestamp: float
    output_preview: str = ""


class ToolGuardrails:
    """工具调用护栏"""

    def __init__(self):
        self._call_history: list[ToolCallRecord] = []
        self._lock = asyncio.Lock()
        self._max_history = 20  # 保留最近 20 次调用记录
        self._exact_failure_warn_after = 2  # 同工具同参数连续失败 2 次警告
        self._exact_failure_halt_after = 3  # 同工具同参数连续失败 3 次硬停止
        self._same_tool_failure_halt_after = 8  # 同工具连续失败 8 次硬停止
        self._no_progress_halt_after = 8  # 8 次调用无进展时中断

    async def record_call(self, tool_name: str, arguments: dict,
                    success: bool, output: str = ""):
        """记录工具调用"""
        args_hash = self._simple_args_hash(arguments)
        record = ToolCallRecord(
            tool_name=tool_name,
            arguments_hash=args_hash,
            success=success,
            timestamp=time.time(),
            output_preview=output[:100] if output else "",
        )
        async with self._lock:
            self._call_history.append(record)
            if len(self._call_history) > self._max_history:
                self._call_history = self._call_history[-self._max_history:]

    async def check(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        """检查是否应该继续执行

        Returns:
            (action, message): action 为 "allow"/"warn"/"halt"，message 为提示信息
        """
        args_hash = self._simple_args_hash(arguments)
        async with self._lock:
            recent = self._call_history[-20:] if self._call_history else []

        # 1. 精确失败重复检测：同工具同参数连续失败
        exact_failures = 0
        for r in reversed(recent):
            if r.tool_name == tool_name and r.arguments_hash == args_hash and not r.success:
                exact_failures += 1
            else:
                break

        if exact_failures >= self._exact_failure_halt_after:
            msg = f"工具 {tool_name} 以相同参数连续失败 {exact_failures} 次，已硬停止。请换一种方法或检查参数。"
            logger.warning("guardrail.exact_failure_halt", tool=tool_name, failures=exact_failures)
            return "halt", msg

        if exact_failures >= self._exact_failure_warn_after:
            msg = f"工具 {tool_name} 以相同参数已连续失败 {exact_failures} 次，建议更换策略。"
            logger.warning("guardrail.exact_failure_warn", tool=tool_name, failures=exact_failures)
            return "warn", msg

        # 2. 同工具失败重复检测
        same_tool_failures = sum(1 for r in recent if r.tool_name == tool_name and not r.success)
        if same_tool_failures >= self._same_tool_failure_halt_after:
            msg = f"工具 {tool_name} 已累计失败 {same_tool_failures} 次，已硬停止。请换一种方法。"
            logger.warning("guardrail.same_tool_halt", tool=tool_name, failures=same_tool_failures)
            return "halt", msg

        # 3. 无进展循环检测：最近 N 次调用都没有新的成功输出
        if len(recent) >= self._no_progress_halt_after:
            last_n = recent[-self._no_progress_halt_after:]
            successes = sum(1 for r in last_n if r.success)
            if successes == 0:
                msg = f"最近 {self._no_progress_halt_after} 次工具调用均未成功，检测到无进展循环。请总结当前发现并换一种方法。"
                logger.warning("guardrail.no_progress_halt", recent_calls=len(last_n))
                return "halt", msg

        return "allow", ""

    def _simple_args_hash(self, arguments: dict) -> str:
        """参数哈希（使用 hashlib.md5 生成更可靠的哈希）"""
        if not arguments:
            return ""
        items = sorted(arguments.items())
        raw = str(items)
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def reset(self):
        """重置调用历史"""
        self._call_history.clear()

    def get_stats(self) -> dict:
        """获取护栏统计"""
        recent = self._call_history[-20:]
        return {
            "total_calls": len(self._call_history),
            "recent_successes": sum(1 for r in recent if r.success),
            "recent_failures": sum(1 for r in recent if not r.success),
        }


# 全局实例
_default_guardrails: ToolGuardrails | None = None

def get_tool_guardrails() -> ToolGuardrails:
    global _default_guardrails
    if _default_guardrails is None:
        _default_guardrails = ToolGuardrails()
    return _default_guardrails
