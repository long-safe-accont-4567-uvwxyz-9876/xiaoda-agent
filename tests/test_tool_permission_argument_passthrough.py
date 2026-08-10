from unittest.mock import MagicMock

from loguru import logger

from security.permission_manager import PermissionMode, get_permission_manager
from tool_engine.tool_executor import ToolExecutor
from tool_engine.tool_registry import ToolResult


async def test_executor_passes_arguments_to_permission_manager(monkeypatch):
    tool = {"enabled": True, "func": MagicMock(), "permission": "execute"}
    monkeypatch.setattr("tool_engine.tool_executor.get_tool", lambda _: tool)
    permission_manager = MagicMock()
    permission_manager.check_tool_permission.return_value = (False, "blocked")
    monkeypatch.setattr(
        "security.permission_manager.get_permission_manager",
        lambda: permission_manager,
    )
    executor = ToolExecutor()
    monkeypatch.setattr(executor, "_enforce_sandbox", lambda *_: "")
    monkeypatch.setattr(executor, "_enforce_workspace_boundary", lambda *_: "")
    arguments = {"command": "rm -rf /"}

    result = await executor.execute("shell_command", arguments)

    assert isinstance(result, ToolResult)
    assert result.success is False
    permission_manager.check_tool_permission.assert_called_once_with(
        "shell_command", arguments
    )
    executor.close()


async def test_auto_mode_dangerous_command_is_blocked_through_executor(monkeypatch):
    tool = {"enabled": True, "func": MagicMock(), "permission": "execute"}
    monkeypatch.setattr("tool_engine.tool_executor.get_tool", lambda _: tool)
    manager = get_permission_manager()
    manager.set_mode(PermissionMode.AUTO)
    executor = ToolExecutor()

    result = await executor.execute("shell_command", {"command": "rm -rf /"})

    assert result.success is False
    executor.close()


async def test_permission_event_logs_argument_keys_without_sensitive_values(monkeypatch):
    tool = {"enabled": True, "func": MagicMock(), "permission": "execute"}
    monkeypatch.setattr("tool_engine.tool_executor.get_tool", lambda _: tool)
    permission_manager = MagicMock()
    permission_manager.check_tool_permission.return_value = (False, "dangerous")
    monkeypatch.setattr(
        "security.permission_manager.get_permission_manager",
        lambda: permission_manager,
    )
    executor = ToolExecutor()
    monkeypatch.setattr(executor, "_enforce_sandbox", lambda *_: "")
    monkeypatch.setattr(executor, "_enforce_workspace_boundary", lambda *_: "")
    events = []
    sink_id = logger.add(lambda message: events.append(message.record), level="DEBUG")
    arguments = {"command": "secret-command", "password": "secret-password"}
    try:
        await executor.execute("shell_command", arguments, user_id="qq_owner")
    finally:
        executor.close()
        logger.remove(sink_id)

    permission_manager.check_tool_permission.assert_called_once_with(
        "shell_command", arguments
    )
    record = next(
        item for item in events
        if item["message"] == "tool.permission_checked"
    )
    assert record["extra"]["tool"] == "shell_command"
    assert record["extra"]["argument_keys"] == ["command", "password"]
    assert record["extra"]["allowed"] is False
    assert "secret-command" not in str(record)
    assert "secret-password" not in str(record)
