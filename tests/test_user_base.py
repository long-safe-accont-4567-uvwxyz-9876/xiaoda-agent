"""UserBase 基类测试。"""
import pytest
from agent_core.user_base import UserBase, AGENT_DISPLAY, STATUS_ICON
from core.event_bus import AgentEvent, AgentEventType


def test_agent_display_contains_all_agents():
    """AGENT_DISPLAY 包含所有子代理的显示名。"""
    assert AGENT_DISPLAY["xiaoli"] == "小莉"
    assert AGENT_DISPLAY["xiaolang"] == "小狼"
    assert AGENT_DISPLAY["xiaolian"] == "小涟"
    assert AGENT_DISPLAY["xiaoke"] == "小可"
    assert AGENT_DISPLAY["xiaoda"] == "小妲"


def test_status_icon_contains_all_event_types():
    """STATUS_ICON 包含所有事件类型的图标。"""
    assert STATUS_ICON["sub_started"] == "🔄"
    assert STATUS_ICON["sub_completed"] == "✅"
    assert STATUS_ICON["sub_failed"] == "❌"
    assert STATUS_ICON["sub_cancelled"] == "🚫"
    assert STATUS_ICON["tool_started"] == "🔧"
    assert STATUS_ICON["tool_completed"] == "✓"
    assert STATUS_ICON["tool_failed"] == "✗"


def test_userbase_is_abstract():
    """UserBase 是抽象类，不能直接实例化。"""
    with pytest.raises(TypeError):
        UserBase()
