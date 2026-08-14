"""agent_dispatcher 工具名常量与排除集合 helper 的单元测试。"""
from __future__ import annotations

from agent_dispatcher import (
    SUB_AGENT_EXTRA_TOOLS,
    SUB_AGENT_MEMORY_TOOL,
    SUB_AGENT_MESSAGE_TOOL,
    SUB_AGENT_PROFILE_TOOLS,
    SubAgent,
)


def test_profile_tools_constant():
    assert SUB_AGENT_PROFILE_TOOLS == {"profile_get", "profile_set", "profile_history", "profile_forget"}


def test_extra_tools_constant():
    assert SUB_AGENT_MEMORY_TOOL == "submit_memory"
    assert SUB_AGENT_MESSAGE_TOOL == "send_message_to_agent"
    assert SUB_AGENT_EXTRA_TOOLS == {"submit_memory", "send_message_to_agent"}


def test_excluded_tool_names_merges_config_excluded():
    class _Cfg:
        excluded_tools = {"web_search"}

    agent = SubAgent.__new__(SubAgent)
    agent.config = _Cfg()
    assert agent._excluded_tool_names() == {
        "web_search", "profile_get", "profile_set", "profile_history", "profile_forget"
    }
