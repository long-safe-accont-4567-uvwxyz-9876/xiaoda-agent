from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_core.tool_executor_mixin import ToolExecutorMixin
from hooks import HookResult
from tool_engine.tool_registry import ToolResult


@pytest.mark.asyncio
async def test_hook_modified_args_are_validated_recorded_and_executed(monkeypatch):
    core = ToolExecutorMixin()
    core._hook_engine = SimpleNamespace(
        fire_pre_tool_use=AsyncMock(
            return_value=HookResult(allowed=True, modified_args={"query": "repaired"})
        ),
        fire_post_tool_use=AsyncMock(return_value=HookResult()),
    )
    core._notify_status = AsyncMock()
    core.tool_executor = SimpleNamespace(execute=AsyncMock(return_value=ToolResult.ok("ok")))
    core._cognitive_state = object()
    core._circuit_breaker = SimpleNamespace(on_success=lambda *args, **kwargs: None)
    guardrails = SimpleNamespace(
        validate_args=lambda tool, args: (args.get("query") == "repaired", "wrong args"),
        check=AsyncMock(return_value=("allow", "")),
        record_call=AsyncMock(),
    )
    monkeypatch.setattr("tool_engine.tool_guardrails.get_tool_guardrails", lambda: guardrails)

    result = await core._execute_tool_with_hooks("web_search", {}, user_input="搜索")

    assert result.success
    core.tool_executor.execute.assert_awaited_once_with(
        "web_search", {"query": "repaired"}, "", False
    )
    guardrails.record_call.assert_awaited_once()
    assert guardrails.record_call.await_args.args[1] == {"query": "repaired"}
