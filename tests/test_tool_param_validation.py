"""测试工具必填参数校验

验证：
1. 当 LLM 没传必填参数时，工具执行器应返回友好错误而非 TypeError
2. 参数校验应在实际执行前拦截
"""
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path
import inspect

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


class TestToolParameterValidation:
    """测试工具执行器的必填参数校验"""

    def test_missing_required_param_returns_friendly_error(self):
        """当 LLM 没传必填参数时，应返回友好错误而非 TypeError"""
        from tool_engine.tool_executor import ToolExecutor, ToolResult

        async def recall(query: str, top_k: int = 5):
            return ToolResult.ok(f"result for {query}")

        tool = {
            "name": "test_recall_mock_param_v2",
            "func": recall,
            "enabled": True,
        }

        executor = ToolExecutor.__new__(ToolExecutor)
        executor.TOOL_TIMEOUTS = {}
        executor._global_timeout = 30

        async def run():
            return await executor._execute_with_timeout(tool, {})

        result = asyncio.run(run())

        assert not result.success
        assert "query" in result.error or "参数" in result.error or "缺少" in result.error

    def test_missing_param_does_not_raise_typeerror(self):
        """缺少必填参数不应抛出 TypeError"""
        from tool_engine.tool_executor import ToolExecutor, ToolResult

        async def search(query: str):
            return ToolResult.ok(query)

        tool = {"name": "test_search_mock_param_v2", "func": search, "enabled": True}
        executor = ToolExecutor.__new__(ToolExecutor)
        executor.TOOL_TIMEOUTS = {}
        executor._global_timeout = 30

        async def run():
            return await executor._execute_with_timeout(tool, {})

        result = asyncio.run(run())
        assert not result.success
        assert "TypeError" not in result.error

    @pytest.mark.asyncio
    async def test_valid_params_still_work(self):
        """有正确参数时工具正常执行"""
        from tool_engine.tool_executor import ToolExecutor, ToolResult

        async def recall(query: str, top_k: int = 5):
            return ToolResult.ok(f"found: {query}")

        tool = {"name": "test_recall_mock_valid_v2", "func": recall, "enabled": True}
        executor = ToolExecutor.__new__(ToolExecutor)
        executor.TOOL_TIMEOUTS = {}
        executor._global_timeout = 30

        result = await executor._execute_with_timeout(tool, {"query": "test"})
        assert result.success
        assert "found: test" in result.data
