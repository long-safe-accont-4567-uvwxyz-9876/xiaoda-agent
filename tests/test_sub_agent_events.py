"""子代理 dispatch 事件发射 + 信念反馈测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.event_bus import event_bus, AgentEvent, AgentEventType


class FakeUser:
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def deliver(self, event: AgentEvent) -> None:
        self.events.append(event)


def _make_mock_mgr(belief_router=None, dispatch_return="这是小狼的回复", dispatch_error=None):
    """创建 mock SubAgentManagerMixin"""
    from agent_core.sub_agent_manager import SubAgentManagerMixin
    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    mgr.dispatcher = MagicMock()
    mgr.dispatcher.get_agent = MagicMock(return_value=MagicMock(
        available=True, config=MagicMock(display_name="小狼")
    ))
    if dispatch_error:
        mgr.dispatcher.dispatch = AsyncMock(side_effect=dispatch_error)
    else:
        mgr.dispatcher.dispatch = AsyncMock(return_value=dispatch_return)
    mgr.context = MagicMock()
    mgr.context.current_address_term = ""
    mgr.context.add_message = AsyncMock()
    mgr.context.belief_router = belief_router
    mgr._build_sub_agent_context = MagicMock(return_value="")
    mgr._bg_task_manager = MagicMock()
    mgr._bg_task_manager.run_background_tasks = MagicMock()
    mgr._voice_mode = False
    mgr._finalize_reply = MagicMock(side_effect=lambda x, **kw: x)
    mgr.security = MagicMock()
    mgr.security.is_owner = MagicMock(return_value=True)
    mgr.get_sticker_manager = MagicMock(return_value=MagicMock(available=False))
    return mgr


@pytest.mark.asyncio
async def test_dispatch_single_emits_started_and_completed():
    """_dispatch_single_sub_agent 发射 SUB_STARTED + SUB_COMPLETED"""
    user = FakeUser()
    token = event_bus.bind_user(user)
    try:
        mgr = _make_mock_mgr()
        await mgr._dispatch_single_sub_agent(
            target="xiaolang", clean_input="写个函数",
            user_id="test", source="test", session_id="s1", trace=MagicMock(),
        )
        types = [e.type for e in user.events]
        assert AgentEventType.SUB_STARTED in types
        assert AgentEventType.SUB_COMPLETED in types
    finally:
        event_bus.unbind_user(token)


@pytest.mark.asyncio
async def test_dispatch_single_emits_failed_on_exception():
    """dispatch 异常时发射 SUB_FAILED"""
    user = FakeUser()
    token = event_bus.bind_user(user)
    try:
        mgr = _make_mock_mgr(dispatch_error=RuntimeError("timeout"))
        await mgr._dispatch_single_sub_agent(
            target="xiaolang", clean_input="写个函数",
            user_id="test", source="test", session_id="s1", trace=MagicMock(),
        )
        types = [e.type for e in user.events]
        assert AgentEventType.SUB_STARTED in types
        assert AgentEventType.SUB_FAILED in types
    finally:
        event_bus.unbind_user(token)


@pytest.mark.asyncio
async def test_dispatch_success_updates_belief():
    """子代理 dispatch 成功后调用 update_belief(success=True)"""
    belief_router = MagicMock()
    belief_router.update_belief = MagicMock()
    event_bus.unbind_user()
    mgr = _make_mock_mgr(belief_router=belief_router, dispatch_return="小狼的回复")
    await mgr._dispatch_single_sub_agent(
        target="xiaolang", clean_input="写个函数",
        user_id="test", source="test", session_id="s1", trace=MagicMock(),
    )
    belief_router.update_belief.assert_called_once_with("xiaolang", True)


@pytest.mark.asyncio
async def test_dispatch_failure_updates_belief():
    """子代理 dispatch 异常后调用 update_belief(success=False)"""
    belief_router = MagicMock()
    belief_router.update_belief = MagicMock()
    event_bus.unbind_user()
    mgr = _make_mock_mgr(belief_router=belief_router, dispatch_error=RuntimeError("timeout"))
    await mgr._dispatch_single_sub_agent(
        target="xiaolang", clean_input="写个函数",
        user_id="test", source="test", session_id="s1", trace=MagicMock(),
    )
    belief_router.update_belief.assert_called_once_with("xiaolang", False)