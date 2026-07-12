"""CLIUser 测试 — 每个事件实时打印。"""
import pytest
from agent_core.user_cli import CLIUser
from core.event_bus import AgentEvent, AgentEventType


@pytest.mark.asyncio
async def test_cli_user_sub_started_prints(capsys):
    """SUB_STARTED 事件打印 🔄 {display}正在思考..."""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="t1",
        data={"display_name": "小狼"},
    ))
    captured = capsys.readouterr()
    assert "🔄" in captured.out
    assert "小狼" in captured.out
    assert "思考" in captured.out


@pytest.mark.asyncio
async def test_cli_user_sub_completed_prints(capsys):
    """SUB_COMPLETED 事件打印 ✅ {display}回复完成"""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="t2",
    ))
    captured = capsys.readouterr()
    assert "✅" in captured.out
    assert "小莉" in captured.out
    assert "完成" in captured.out


@pytest.mark.asyncio
async def test_cli_user_tool_started_prints(capsys):
    """TOOL_STARTED 事件打印 🔧 正在调用{tool}..."""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_STARTED,
        agent="xiaoke",
        task_id="t3",
        data={"tool_name": "web_search"},
    ))
    captured = capsys.readouterr()
    assert "🔧" in captured.out
    assert "web_search" in captured.out


@pytest.mark.asyncio
async def test_cli_user_uses_agent_display_fallback(capsys):
    """没有 display_name 时使用 AGENT_DISPLAY 映射。"""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaoda",
        task_id="t4",
    ))
    captured = capsys.readouterr()
    assert "小妲" in captured.out
