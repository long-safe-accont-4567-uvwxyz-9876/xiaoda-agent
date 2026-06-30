from typing import Any, Optional
import asyncio
import json
import time
import inspect
from loguru import logger

from .tool_registry import get_tool, ToolPermission, ToolResult, resolve_tool_func
from utils.metrics import metrics

# 敏感参数关键词，匹配到的参数值会被屏蔽
_SENSITIVE_KEYS = {'key', 'token', 'password', 'secret', 'api_key', 'credential'}


def _filter_sensitive_args(arguments: dict) -> dict:
    """过滤敏感参数值，保留参数名，值替换为 ***REDACTED***"""
    filtered = {}
    for k, v in arguments.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            filtered[k] = '***REDACTED***'
        else:
            filtered[k] = v
    return filtered


class ToolExecutor:

    # 按工具名自定义超时（秒），default 为全局默认
    TOOL_TIMEOUTS: dict[str, float] = {
        "agnes_video_generate": 240,
        "document_reader": 120,
        "web_browse": 30,           # 网页渲染较慢
        "multi_search": 25,         # 多引擎并发搜索
        "web_search": 15,           # 单次网络搜索
        "wolfram_query": 20,        # 知识计算引擎
        "python_executor": 30,      # 代码执行可能较慢
        "shell_command": 20,        # Shell 命令执行
        "delegate_task": 60,        # 子代理委托
        "default": 60.0,
    }

    def __init__(self, db: Optional[Any]=None) -> None:
        self.db = db
        self._call_counts: dict[str, list[float]] = {}
        self._global_timeout: float = self.TOOL_TIMEOUTS["default"]

    async def execute(self, tool_name: str, arguments: dict,
                      user_id: str = "", safe_mode: bool = False) -> ToolResult:
        tool = get_tool(tool_name)
        if not tool:
            logger.warning("tool_executor.not_found", tool=tool_name)
            return ToolResult.fail(f"还没有学会「{tool_name}」这个技能呢……")

        if tool.get("enabled") is False:
            logger.warning("tool_executor.disabled", tool=tool_name)
            return ToolResult.fail(f"「{tool_name}」已被管理员全局停用了呢～")

        if not self._check_rate_limit(tool_name, tool):
            logger.warning("tool_executor.rate_limited", tool=tool_name)
            return ToolResult.fail("刚才已经帮你查过了呢……等一会儿再看好不好？")

        _start = time.time()
        result = await self._execute_with_timeout(tool, arguments)
        duration = time.time() - _start
        metrics.observe(f"tool_execute.{tool_name}.duration", duration)
        if result.success:
            metrics.inc(f"tool_execute.{tool_name}.success")
        else:
            metrics.inc(f"tool_execute.{tool_name}.failure")
        metrics.maybe_report()
        # 结构化日志：工具执行结果
        logger.info("tool.execute", event="tool_execute", tool=tool_name,
                    duration_ms=int(duration * 1000), user_id=user_id,
                    success=result.success)

        # A4: 工具执行结果 → 学习反馈闭环 (失败不阻塞主流程)
        try:
            from core.learning_feedback import record_tool_outcome
            record_tool_outcome(
                tool_name=tool_name,
                arguments=arguments,
                success=result.success,
                error=result.error or "",
                duration=duration,
            )
        except Exception as _e:
            logger.debug(f"tool_executor.learning_feedback_failed: {_e}")

        if self.db:
            await self._write_audit_log(tool_name, arguments, result, user_id)

        return result

    def _check_rate_limit(self, tool_name: str, tool: dict) -> bool:
        max_freq = tool.get("max_frequency", 6000)
        if max_freq == 0:
            return True

        now = time.time()
        window = 10
        timestamps = self._call_counts.get(tool_name, [])
        timestamps = [t for t in timestamps if now - t < window]
        self._call_counts[tool_name] = timestamps

        if len(timestamps) >= max_freq:
            return False
        timestamps.append(now)
        return True

    async def _execute_with_timeout(self, tool: dict, arguments: dict) -> ToolResult:
        func, lazy_err = resolve_tool_func(tool)
        if func is None:
            return ToolResult.fail(lazy_err or f"工具「{tool.get('name')}」实现未加载")
        tool_name = tool["name"]
        timeout = self.TOOL_TIMEOUTS.get(tool_name, self._global_timeout)

        try:
            sig = inspect.signature(func)
            call_args = dict(arguments)

            if asyncio.iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(**call_args) if call_args else func(),
                    timeout=timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, **call_args) if call_args else asyncio.to_thread(func),
                    timeout=timeout,
                )

            if isinstance(result, ToolResult):
                return result
            return ToolResult.ok(result)
        except asyncio.TimeoutError:
            logger.error("tool_executor.timeout", tool=tool_name, timeout=timeout)
            metrics.inc(f"tool.timeout.{tool_name}")
            return ToolResult.fail("那边有点慢呢……等会儿再试试好不好？")
        except Exception as e:
            logger.error("tool_executor.error", tool=tool_name, error=str(e))
            return ToolResult.fail(f"出了一点小问题……等会儿再试试好不好？")

    async def _write_audit_log(self, tool_name: str, arguments: dict,
                               result: ToolResult, user_id: str) -> None:
        try:
            # 安全加固：过滤敏感参数值
            safe_args = _filter_sensitive_args(arguments)
            await self.db.insert_audit_log(
                event_type="tool_call",
                user_id=user_id,
                detail=json.dumps({
                    "tool": tool_name,
                    "arguments": safe_args,
                    "success": result.success,
                    "error": result.error[:200] if result.error else "",
                }, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning("tool_executor.audit_log_failed", error=str(e))
