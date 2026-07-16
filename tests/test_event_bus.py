"""EventBus 核心测试 — 定向投递给 User，非订阅/广播。"""
import pytest
from core.event_bus import event_bus, AgentEvent, AgentEventType, gen_task_id


@pytest.fixture(autouse=True)
def _reset_event_bus():
    from core.event_bus import event_bus
    event_bus.unbind_user()
    yield
    event_bus.unbind_user()


class FakeUser:
    """测试用 FakeUser，记录收到的所有事件。"""
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def deliver(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_emit_without_user_silently_ignores():
    """没有绑定 User 时，emit 静默忽略，不抛异常。"""
    event_bus.unbind_user()  # 确保无绑定
    await event_bus.emit(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="test_123",
    ))
    # 无异常即通过


@pytest.mark.asyncio
async def test_emit_delivers_to_bound_user():
    """emit 后事件投递给绑定的 User。"""
    user = FakeUser()
    token = event_bus.bind_user(user)
    try:
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent="xiaolang",
            task_id="test_456",
            data={"display_name": "小狼"},
        ))
        assert len(user.events) == 1
        assert user.events[0].type == AgentEventType.SUB_STARTED
        assert user.events[0].agent == "xiaolang"
        assert user.events[0].data["display_name"] == "小狼"
    finally:
        event_bus.unbind_user(token)


@pytest.mark.asyncio
async def test_unbind_stops_delivery():
    """unbind_user 后不再投递。"""
    user = FakeUser()
    token = event_bus.bind_user(user)
    event_bus.unbind_user(token)
    await event_bus.emit(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="test_789",
    ))
    assert len(user.events) == 0


@pytest.mark.asyncio
async def test_emit_user_deliver_error_does_not_raise():
    """User.deliver() 异常不中断调用方。"""
    class BrokenUser:
        async def deliver(self, event):
            raise RuntimeError("broken")

    token = event_bus.bind_user(BrokenUser())
    try:
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent="xiaoke",
            task_id="test_000",
        ))
        # 无异常即通过
    finally:
        event_bus.unbind_user(token)


def test_gen_task_id_format():
    """gen_task_id 返回 {agent}_{8hex} 格式。"""
    task_id = gen_task_id("xiaolang")
    assert task_id.startswith("xiaolang_")
    assert len(task_id) == len("xiaolang_") + 8


def test_event_type_values():
    """AgentEventType 枚举值正确。"""
    assert AgentEventType.SUB_STARTED == "sub_started"
    assert AgentEventType.SUB_COMPLETED == "sub_completed"
    assert AgentEventType.SUB_FAILED == "sub_failed"
    assert AgentEventType.SUB_CANCELLED == "sub_cancelled"
    assert AgentEventType.TOOL_STARTED == "tool_started"
    assert AgentEventType.TOOL_COMPLETED == "tool_completed"
    assert AgentEventType.TOOL_FAILED == "tool_failed"