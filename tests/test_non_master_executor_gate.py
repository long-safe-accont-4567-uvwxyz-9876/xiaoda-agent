"""TDD 测试：非主人工具门禁必须在执行层（ToolCallHandler）强制生效（VULN-27）。

背景：非主人 QQ 群消息 @子代理（如 @小莉）走 _dispatch_single_sub_agent →
dispatcher.chat() → _filtered_tools()，该路径不经过主路径的
ALLOWED_NON_MASTER_TOOLS 工具列表过滤，非主人可通过子代理通道触发
shell_command / write_file 等 EXECUTE 工具。

修复策略：白名单从"工具列表过滤"（单层、易漏）下沉为执行层强制 ——
ToolCallHandler._execute_single_tool 在执行前读取 _current_request_ctx，
非主人（ctx.is_master=False）调用白名单外工具直接拒绝。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent_core._shared import RequestContext, _current_request_ctx
from agent_core.message_processor import MessageProcessorMixin
from tool_engine.tool_call_handler import ToolCallHandler


class _StubRepair:
    def detect_storm(self, name: str, args: str) -> bool:
        return False

    def repair_truncation(self, args: str) -> str:
        return ""


def _make_handler() -> ToolCallHandler:
    """构造仅用于门禁校验的 handler（不依赖完整运行时）。"""
    return ToolCallHandler(
        tool_executor=None,
        tool_repair=_StubRepair(),
        clean_reply_callback=lambda x: x,
    )


def _tool_call(name: str, args: dict | None = None) -> dict:
    import json
    return {
        "id": "call_test_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


def _non_master_ctx() -> RequestContext:
    return RequestContext(session_id="s1", user_id="qq_stranger", is_master=False)


def _master_ctx() -> RequestContext:
    return RequestContext(session_id="s1", user_id="qq_owner", is_master=True)


@pytest.mark.asyncio
async def test_non_master_tool_gate_rejects_execute_tools():
    """非主人请求上下文中，白名单外的 EXECUTE 工具必须被拒绝（不触达执行器）"""
    handler = _make_handler()
    executed: list[str] = []

    async def _fake_exec(name, args, **kw):
        executed.append(name)
        from tool_engine.tool_executor import ToolResult
        return ToolResult.ok("should not reach here")

    handler._tool_executor = type("E", (), {"execute": staticmethod(_fake_exec)})()

    token = _current_request_ctx.set(_non_master_ctx())
    try:
        for tool in ("shell_command", "python_executor", "write_file", "delete_file"):
            executed.clear()
            tc_id, result, text, _ = await handler._execute_single_tool(
                _tool_call(tool, {"command": "ls"} if tool == "shell_command" else {}), None)
            assert not result.success, f"非主人调用 {tool} 应被拒绝"
            assert not executed, f"{tool} 不应触达执行器"
    finally:
        _current_request_ctx.reset(token)


@pytest.mark.asyncio
async def test_non_master_tool_gate_allows_whitelisted_tools():
    """非主人仍可调用白名单内工具（web_search 等）"""
    from tool_engine.tool_executor import ToolResult

    handler = _make_handler()

    async def _fake_exec(name, args, **kw):
        return ToolResult.ok("ok")

    handler._tool_executor = type("E", (), {"execute": staticmethod(_fake_exec)})()

    token = _current_request_ctx.set(_non_master_ctx())
    try:
        for tool in ("web_search", "get_current_time", "calculator"):
            _, result, _, _ = await handler._execute_single_tool(_tool_call(tool), None)
            assert result.success, f"白名单工具 {tool} 应放行"
    finally:
        _current_request_ctx.reset(token)


@pytest.mark.asyncio
async def test_master_not_affected_by_gate():
    """主人请求不受门禁影响（可执行 EXECUTE 工具）"""
    from tool_engine.tool_executor import ToolResult

    handler = _make_handler()

    async def _fake_exec(name, args, **kw):
        return ToolResult.ok("done")

    handler._tool_executor = type("E", (), {"execute": staticmethod(_fake_exec)})()

    token = _current_request_ctx.set(_master_ctx())
    try:
        _, result, _, _ = await handler._execute_single_tool(
            _tool_call("shell_command", {"command": "ls"}), None)
        assert result.success
    finally:
        _current_request_ctx.reset(token)


@pytest.mark.asyncio
async def test_no_ctx_fails_open_for_owner_only_channels():
    """无请求上下文（内部任务/调度器）不拦截——门禁只约束带身份的消息请求"""
    from tool_engine.tool_executor import ToolResult

    handler = _make_handler()

    async def _fake_exec(name, args, **kw):
        return ToolResult.ok("done")

    handler._tool_executor = type("E", (), {"execute": staticmethod(_fake_exec)})()

    assert _current_request_ctx.get() is None
    _, result, _, _ = await handler._execute_single_tool(
        _tool_call("shell_command", {"command": "ls"}), None)
    assert result.success


def test_whitelist_single_source():
    """白名单唯一定义在 agent_core._shared，message_processor 引用同一对象（防两处漂移）"""
    from agent_core._shared import ALLOWED_NON_MASTER_TOOLS
    assert MessageProcessorMixin.ALLOWED_NON_MASTER_TOOLS is ALLOWED_NON_MASTER_TOOLS
