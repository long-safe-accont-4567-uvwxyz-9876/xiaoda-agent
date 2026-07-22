"""QQUser 测试 — 仅 SUB_STARTED 通知，其余静默。"""
import pytest

from agent_core.user_qq import QQUser
from core.event_bus import AgentEvent, AgentEventType


@pytest.mark.asyncio
async def test_qq_user_sub_started_sends_message():
    """SUB_STARTED 发送1条消息：🔄 {display}正在思考..."""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="t1",
        data={"display_name": "小狼"},
    ))
    assert len(sent) == 1
    assert "🔄" in sent[0]
    assert "小狼" in sent[0]
    assert "思考" in sent[0]


@pytest.mark.asyncio
async def test_qq_user_sub_completed_silent():
    """SUB_COMPLETED 不发送消息（节省消息条数）。"""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="t2",
    ))
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_qq_user_tool_events_silent():
    """TOOL_* 事件不发送消息。"""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_STARTED,
        agent="xiaoke",
        task_id="t3",
        data={"tool_name": "web_search"},
    ))
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_COMPLETED,
        agent="xiaoke",
        task_id="t3",
        data={"tool_name": "web_search"},
    ))
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_qq_user_sub_failed_silent():
    """SUB_FAILED 不发送消息（主回复会包含降级文案）。"""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_FAILED,
        agent="xiaolian",
        task_id="t4",
    ))
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_qq_user_reply_error_does_not_raise():
    """reply_fn 异常不中断调用方。"""
    async def broken_reply(content: str, msg_seq: int = 0) -> None:
        raise RuntimeError("qq rate limited")

    user = QQUser(reply_fn=broken_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaoda",
        task_id="t5",
    ))
    # 无异常即通过
