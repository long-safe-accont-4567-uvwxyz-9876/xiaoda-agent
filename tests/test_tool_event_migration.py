"""工具事件迁移到 EventBus 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.event_bus import event_bus, AgentEvent, AgentEventType


class FakeUser:
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def deliver(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_notify_tool_status_started_emits_event():
    """_notify_tool_status(stage='started') 发射 TOOL_STARTED 事件。"""
    from tool_engine.tool_call_handler import ToolCallHandler

    user = FakeUser()
    token = event_bus.bind_user(user)
    try:
        handler = ToolCallHandler.__new__(ToolCallHandler)
        handler._status_callback = None
        handler._agent_name = "xiaoke"
        # Need to patch STREAM_TOOL_STATUS to True
        with patch('config.STREAM_TOOL_STATUS', True):
            await handler._notify_tool_status("web_search", "started")

        tool_events = [e for e in user.events if e.type == AgentEventType.TOOL_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].data["tool_name"] == "web_search"
    finally:
        event_bus.unbind_user(token)


@pytest.mark.asyncio
async def test_notify_tool_status_completed_emits_event():
    """_notify_tool_status(stage='completed') 发射 TOOL_COMPLETED 事件。"""
    from tool_engine.tool_call_handler import ToolCallHandler

    user = FakeUser()
    token = event_bus.bind_user(user)
    try:
        handler = ToolCallHandler.__new__(ToolCallHandler)
        handler._status_callback = None
        handler._agent_name = "xiaoke"
        with patch('config.STREAM_TOOL_STATUS', True):
            await handler._notify_tool_status("web_search", "completed", "found 3 results")

        tool_events = [e for e in user.events if e.type == AgentEventType.TOOL_COMPLETED]
        assert len(tool_events) == 1
        assert tool_events[0].data["tool_name"] == "web_search"
    finally:
        event_bus.unbind_user(token)


@pytest.mark.asyncio
async def test_notify_tool_status_failed_emits_event():
    """_notify_tool_status(stage='failed') 发射 TOOL_FAILED 事件。"""
    from tool_engine.tool_call_handler import ToolCallHandler

    user = FakeUser()
    token = event_bus.bind_user(user)
    try:
        handler = ToolCallHandler.__new__(ToolCallHandler)
        handler._status_callback = None
        handler._agent_name = "xiaoke"
        with patch('config.STREAM_TOOL_STATUS', True):
            await handler._notify_tool_status("web_search", "failed", "timeout")

        tool_events = [e for e in user.events if e.type == AgentEventType.TOOL_FAILED]
        assert len(tool_events) == 1
    finally:
        event_bus.unbind_user(token)