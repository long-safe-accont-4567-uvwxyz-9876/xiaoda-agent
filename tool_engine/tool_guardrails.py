"""工具调用护栏 - 检测和阻止工具调用循环 + 参数验证"""
import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from loguru import logger


@dataclass
class ToolCallRecord:
    """工具调用记录"""
    tool_name: str
    arguments_hash: str  # 参数的简化哈希
    success: bool
    timestamp: float
    output_preview: str = ""


# ── L1/L2 参数验证规则 ────────────────────────────────────────
# L1: 格式验证 —— 必填字段、类型检查
# L2: 业务验证 —— 值范围、逻辑约束
_TOOL_VALIDATION_RULES: dict[str, dict] = {
    "shell_command": {
        "required": ["command"],
        "checks": [
            ("command", "non_empty_str", "命令不能为空"),
            ("command", "max_len:2000", "命令过长（>2000字符）"),
        ],
    },
    "web_search": {
        "required": ["query"],
        "checks": [
            ("query", "non_empty_str", "搜索词不能为空"),
            ("query", "max_len:500", "搜索词过长"),
        ],
    },
    "web_browse": {
        "required": ["url"],
        "checks": [
            ("url", "non_empty_str", "URL 不能为空"),
            ("url", "url_format", "URL 格式不合法"),
        ],
    },
    "python_executor": {
        "required": ["code"],
        "checks": [
            ("code", "non_empty_str", "代码不能为空"),
            ("code", "max_len:10000", "代码过长（>10000字符）"),
        ],
    },
    "multi_search": {
        "required": ["queries"],
        "checks": [
            ("queries", "non_empty_list", "搜索词列表不能为空"),
            ("queries", "max_list_len:10", "搜索词过多（>10个）"),
        ],
    },
    "document_reader": {
        "required": ["file_path"],
        "checks": [
            ("file_path", "non_empty_str", "文件路径不能为空"),
            ("file_path", "no_path_traversal", "文件路径包含非法遍历"),
        ],
    },
    "list_files": {
        "required": [],
        "checks": [
            ("path", "no_path_traversal", "目录路径包含非法遍历"),
        ],
    },
    "read_file": {
        "required": ["path"],
        "checks": [
            ("path", "non_empty_str", "文件路径不能为空"),
            ("path", "no_path_traversal", "文件路径包含非法遍历"),
        ],
    },
    "write_file": {
        "required": ["input_str"],
        "checks": [
            ("input_str", "non_empty_str", "输入不能为空"),
            ("input_str", "max_len:50000", "写入内容过长（>50000字符）"),
        ],
    },
    "search_files": {
        "required": ["pattern"],
        "checks": [
            ("pattern", "non_empty_str", "搜索模式不能为空"),
            ("pattern", "max_len:500", "搜索模式过长"),
        ],
    },
    "calculator": {
        "required": ["expression"],
        "checks": [
            ("expression", "non_empty_str", "数学表达式不能为空"),
            ("expression", "max_len:500", "数学表达式过长"),
        ],
    },
    "get_weather": {
        "required": ["city"],
        "checks": [
            ("city", "non_empty_str", "城市名称不能为空"),
            ("city", "max_len:50", "城市名称过长"),
        ],
    },
    "wolfram_query": {
        "required": ["query"],
        "checks": [
            ("query", "non_empty_str", "查询不能为空"),
            ("query", "max_len:500", "查询过长"),
        ],
    },
}

# L3: 一致性检查 —— 禁止危险模式
_DANGEROUS_PATTERNS = [
    (r"rm\s+-rf\s+/", "危险的递归删除命令"),
    (r"curl.*\|\s*sh", "管道执行远程脚本"),
    (r"chmod\s+777", "过于宽松的文件权限"),
    (r">/dev/sd[a-z]", "直接写入磁盘设备"),
]


class ToolGuardrails:
    """工具调用护栏"""

    def __init__(self) -> None:
        self._call_history: list[ToolCallRecord] = []
        self._lock = asyncio.Lock()
        self._max_history = 20  # 保留最近 20 次调用记录
        self._exact_failure_warn_after = 2  # 同工具同参数连续失败 2 次警告
        self._exact_failure_halt_after = 3  # 同工具同参数连续失败 3 次硬停止
        self._same_tool_failure_halt_after = 8  # 同工具连续失败 8 次硬停止
        self._no_progress_halt_after = 8  # 8 次调用无进展时中断

    def validate_args(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """L1/L2/L3 参数验证。

        Returns:
            (valid, reason): valid=True 表示通过，False 表示被拦截。
        """
        try:
            if not isinstance(arguments, dict):
                return False, f"参数类型错误: 期望 dict, 得到 {type(arguments).__name__}"

            # L1+L2: 工具特定规则
            rules = _TOOL_VALIDATION_RULES.get(tool_name)
            if rules:
                ok, reason = self._validate_required_fields(rules, arguments)
                if not ok:
                    return False, reason

                ok, reason = self._validate_field_checks(rules, arguments)
                if not ok:
                    return False, reason

            # L3: 危险模式检查（主要针对 shell_command）
            ok, reason = self._check_dangerous_patterns(tool_name, arguments)
            if not ok:
                return False, reason

            return True, ""
        except Exception as e:
            logger.error(f"validate_args 异常: {e}", tool_name=tool_name)
            return False, f"验证异常: {e}"

    def _validate_required_fields(self, rules: dict, arguments: dict) -> tuple[bool, str]:
        """L1: 必填字段检查。"""
        for field_name in rules.get("required", []):
            val = arguments.get(field_name)
            if val is None or (isinstance(val, str) and not val.strip()):
                err_msg = rules['checks'][0][2] if rules['checks'] else field_name + ' 是必填字段'
                return False, f"L1验证失败: {err_msg}"
        return True, ""

    def _validate_field_checks(self, rules: dict, arguments: dict) -> tuple[bool, str]:
        """L1/L2: 按规则检查字段类型与值范围。"""
        for field_name, check_type, err_msg in rules.get("checks", []):
            val = arguments.get(field_name)
            if val is None:
                continue

            ok, level, reason = self._apply_single_check(val, check_type, err_msg)
            if not ok:
                return False, f"{level}验证失败: {reason}"
        return True, ""

    @staticmethod
    def _apply_single_check(val, check_type: str, err_msg: str) -> tuple[bool, str, str]:
        """应用单个 check 规则，返回 (ok, level, reason)。

        level 为 "L1"/"L2"/"L3" 之一；ok=True 时 level/reason 无意义。
        """
        if check_type == "non_empty_str":
            if not isinstance(val, str) or not val.strip():
                return False, "L1", err_msg
        elif check_type.startswith("max_len:"):
            limit = int(check_type.split(":")[1])
            if isinstance(val, str) and len(val) > limit:
                return False, "L2", err_msg
        elif check_type == "url_format":
            if isinstance(val, str) and not re.match(r"https?://", val):
                return False, "L2", err_msg
        elif check_type == "non_empty_list":
            if not isinstance(val, list) or len(val) == 0:
                return False, "L1", err_msg
        elif check_type.startswith("max_list_len:"):
            limit = int(check_type.split(":")[1])
            if isinstance(val, list) and len(val) > limit:
                return False, "L2", err_msg
        elif check_type == "no_path_traversal" and isinstance(val, str) and ".." in val:
            return False, "L3", err_msg
        return True, "", ""

    @staticmethod
    def _check_dangerous_patterns(tool_name: str, arguments: dict) -> tuple[bool, str]:
        """L3: 危险模式检查（主要针对 shell_command）。"""
        if tool_name != "shell_command":
            return True, ""
        cmd = arguments.get("command", "")
        if not isinstance(cmd, str):
            return True, ""
        for pattern, desc in _DANGEROUS_PATTERNS:
            try:
                if re.search(pattern, cmd):
                    return False, f"L3验证失败: {desc}"
            except re.error:
                logger.warning(f"正则模式异常: {pattern}")
                continue
        return True, ""

    async def record_call(self, tool_name: str, arguments: dict,
                    success: bool, output: str = "") -> None:
        """记录工具调用"""
        try:
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
        except Exception as e:
            logger.error(f"record_call 异常: {e}", tool_name=tool_name)

    async def check(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        """检查是否应该继续执行

        Returns:
            (action, message): action 为 "allow"/"warn"/"halt"，message 为提示信息
        """
        try:
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
        except Exception as e:
            logger.error(f"check 异常: {e}", tool_name=tool_name)
            return "allow", ""  # 异常时默认放行, 避免阻断正常流程

    def _simple_args_hash(self, arguments: dict) -> str:
        """参数哈希（使用 hashlib.md5 生成更可靠的哈希）"""
        try:
            if not arguments:
                return ""
            items = sorted(arguments.items())
            raw = str(items)
            return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:16]
        except Exception:
            return ""

    def reset(self) -> None:
        """重置调用历史"""
        self._call_history.clear()

    def get_stats(self) -> dict:
        """获取护栏统计"""
        try:
            recent = self._call_history[-20:]
            return {
                "total_calls": len(self._call_history),
                "recent_successes": sum(1 for r in recent if r.success),
                "recent_failures": sum(1 for r in recent if not r.success),
            }
        except Exception as e:
            logger.error(f"get_stats 异常: {e}")
            return {"total_calls": 0, "recent_successes": 0, "recent_failures": 0}


# 全局实例
_default_guardrails: ToolGuardrails | None = None

def get_tool_guardrails() -> ToolGuardrails:
    """获取工具护栏全局单例。"""
    global _default_guardrails
    if _default_guardrails is None:
        _default_guardrails = ToolGuardrails()
    return _default_guardrails