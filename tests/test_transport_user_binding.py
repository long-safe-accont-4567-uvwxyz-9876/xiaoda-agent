"""传输层 User 绑定测试。"""
import pytest
from core.event_bus import event_bus


def test_event_bus_has_no_user_after_unbind():
    """unbind 后 bound_user 为 None。"""
    event_bus.unbind_user()
    assert event_bus.bound_user is None


@pytest.mark.asyncio
async def test_qq_adapter_binds_qq_user():
    """QQ 适配器在 process 前 bind QQUser，后 unbind。"""
    from agent_core.user_qq import QQUser

    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    event_bus.bind_user(user)
    assert isinstance(event_bus.bound_user, QQUser)
    event_bus.unbind_user()
    assert event_bus.bound_user is None


@pytest.mark.asyncio
async def test_web_adapter_binds_web_user():
    """Web 适配器 bind WebUser。"""
    from agent_core.user_web import WebUser

    async def fake_send(event: dict) -> None:
        pass

    user = WebUser(send_fn=fake_send)
    event_bus.bind_user(user)
    assert isinstance(event_bus.bound_user, WebUser)
    event_bus.unbind_user()
    assert event_bus.bound_user is None
