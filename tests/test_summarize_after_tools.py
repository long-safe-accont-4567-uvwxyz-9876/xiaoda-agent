"""测试 SubAgent._summarize_after_tools 验证 get_temperature 正确导入。

BUG: _summarize_after_tools 方法中使用了 get_temperature 但未导入
NameError 被 except (TimeoutError, Exception) 静默吞掉
导致 LLM 总结功能永远不执行 总是返回降级内容
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

from agent_dispatcher import SubAgent, SubAgentConfig


def _make_sub_agent():
    """创建最小化 SubAgent 实例"""
    cfg = SubAgentConfig(
        name="xiaoke",
        display_name="小可",
        provider="test",
        model="test-model",
        base_url="http://localhost",
        api_key_env="TEST_KEY",
    )
    agent = SubAgent.__new__(SubAgent)
    agent.config = cfg
    agent._client = MagicMock()
    agent._degraded = False
    agent._initialized = True
    return agent


def _fake_response(content="这是总结回复"):
    """构造 fake LLM 响应"""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].message.reasoning_content = None
    return resp


@pytest.mark.asyncio
async def test_summarize_returns_llm_response():
    """_summarize_after_tools 应返回 LLM 总结 而非降级内容"""
    agent = _make_sub_agent()
    fake_resp = _fake_response("根据查询结果今天天气晴朗")
    agent._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    working = [
        {"role": "user", "content": "今天天气怎么样"},
        {"role": "tool", "content": "晴天25度", "tool_call_id": "tc1"},
    ]

    result = await agent._summarize_after_tools(working, api_timeout=30, remaining=30.0)

    assert "根据查询结果" in result, f"应返回LLM总结 实际: {result}"


@pytest.mark.asyncio
async def test_summarize_api_actually_called():
    """_summarize_after_tools 应成功调用API 不因NameError跳过"""
    agent = _make_sub_agent()
    fake_resp = _fake_response("总结完成")
    agent._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    working = [{"role": "user", "content": "测试"}]

    await agent._summarize_after_tools(working, api_timeout=30, remaining=30.0)

    agent._client.chat.completions.create.assert_awaited_once()
