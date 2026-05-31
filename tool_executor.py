import asyncio
import json
import time
import inspect
from loguru import logger

from tool_registry import get_tool, ToolPermission, ToolResult


class ToolExecutor:

    def __init__(self, db=None):
        self.db = db
        self._call_counts: dict[str, list[float]] = {}
        self._global_timeout: float = 60.0

    async def execute(self, tool_name: str, arguments: dict,
                      user_id: str = "", safe_mode: bool = False) -> ToolResult:
        tool = get_tool(tool_name)
        if not tool:
            return ToolResult.fail(f"还没有学会「{tool_name}」这个技能呢……")

        if not self._check_rate_limit(tool_name, tool):
            return ToolResult.fail("刚才已经帮你查过了呢……等一会儿再看好不好？")

        result = await self._execute_with_timeout(tool, arguments)

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
        func = tool["func"]
        tool_name = tool["name"]
        try:
            call_args = dict(arguments)
            if asyncio.iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(**call_args) if call_args else func(),
                    timeout=self._global_timeout,
                )
            else:
                result = func(**call_args) if call_args else func()
            if isinstance(result, ToolResult):
                return result
            return ToolResult.ok(result)
        except asyncio.TimeoutError:
            return ToolResult.fail("那边有点慢呢……等会儿再试试好不好？")
        except Exception as e:
            return ToolResult.fail(f"出了一点小问题……等会儿再试试好不好？")

    async def _write_audit_log(self, tool_name: str, arguments: dict,
                               result: ToolResult, user_id: str):
        try:
            await self.db.insert_audit_log(
                event_type="tool_call",
                user_id=user_id,
                detail=json.dumps({
                    "tool": tool_name,
                    "arguments": arguments,
                    "success": result.success,
                    "error": result.error[:200] if result.error else "",
                }, ensure_ascii=False),
            )
        except Exception:
            pass
