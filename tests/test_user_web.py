"""WebUser 测试 — 每个事件通过 WebSocket 推送。"""
import pytest
from agent_core.user_web import WebUser
from core.event_bus import AgentEvent, AgentEventType


@pytest.mark.asyncio
async def test_web_user_sub_started_sends_json():
    """SUB_STARTED 事件通过 send_fn 推送 dict。"""
    sent: list[dict] = []

    async def fake_send(event: dict) -> None:
        sent.append(event)

    user = WebUser(send_fn=fake_send)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="t1",
        data={"display_name": "小狼", "input_preview": "写代码"},
    ))
    assert len(sent) == 1
    assert sent[0]["type"] == "sub_started"
    assert sent[0]["agent"] == "xiaolang"
    assert sent[0]["display_name"] == "小狼"


@pytest.mark.asyncio
async def test_web_user_tool_started_sends_json():
    """TOOL_STARTED 事件通过 send_fn 推送 dict。"""
    sent: list[dict] = []

    async def fake_send(event: dict) -> None:
        sent.append(event)

    user = WebUser(send_fn=fake_send)
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_STARTED,
        agent="xiaoke",
        task_id="t2",
        data={"tool_name": "web_search"},
    ))
    assert len(sent) == 1
    assert sent[0]["type"] == "tool_started"
    assert sent[0]["tool_name"] == "web_search"


@pytest.mark.asyncio
async def test_web_user_send_error_does_not_raise():
    """send_fn 异常不中断调用方。"""
    async def broken_send(event: dict) -> None:
        raise RuntimeError("ws closed")

    user = WebUser(send_fn=broken_send)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="t3",
    ))
    # 无异常即通过
