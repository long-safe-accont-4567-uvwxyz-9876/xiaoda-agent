"""子Agent LLM调用超时重试机制测试

验证: 网络抖动导致首次超时时, 用半超时值重试一次;
重试也超时才返回错误提示; 重试次数可配置。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保项目路径
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))

from agent_dispatcher import SubAgent, SubAgentConfig


def _make_sub_agent():
    """创建最小化的 SubAgent 实例 (仅用于测试 _call_llm_one_round)"""
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


def _fake_response():
    """构造一个 fake LLM 响应对象"""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "你好"
    resp.choices[0].message.tool_calls = None
    return resp


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    """首次超时, 重试成功 → 返回响应对象

    策略: 用很短的 timeout (0.2s) + 首次 hang (1s) 触发真实 TimeoutError;
    第二次调用立即返回响应.
    """
    agent = _make_sub_agent()
    fake_resp = _fake_response()

    call_count = {"n": 0}

    async def _fake_create(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            await asyncio.sleep(1.0)  # hang 住触发超时
        return fake_resp

    agent._client.chat.completions.create = AsyncMock(side_effect=_fake_create)

    # patch 配置: timeout=0.2s, retry=1
    with patch("config.SUB_AGENT_API_TIMEOUT", 0.2), \
         patch("config.SUB_AGENT_API_RETRY", 1):
        result = await agent._call_llm_one_round(
            working=[{"role": "user", "content": "hi"}],
            tools=None,
            remaining=120,
            round_idx=0,
        )

    assert result is fake_resp
    assert call_count["n"] == 2  # 调用了 2 次


@pytest.mark.asyncio
async def test_retry_also_timeout_returns_prompt():
    """重试也超时 → 返回用户可见提示"""
    agent = _make_sub_agent()

    call_count = {"n": 0}

    async def _fake_create(**kwargs):
        call_count["n"] += 1
        await asyncio.sleep(1.0)  # 每次都 hang

    agent._client.chat.completions.create = AsyncMock(side_effect=_fake_create)

    with patch("config.SUB_AGENT_API_TIMEOUT", 0.2), \
         patch("config.SUB_AGENT_API_RETRY", 1):
        result = await agent._call_llm_one_round(
            working=[{"role": "user", "content": "hi"}],
            tools=None,
            remaining=120,
            round_idx=0,
        )

    assert isinstance(result, str)
    assert "思考时间太长了" in result
    assert call_count["n"] == 2  # 重试了1次


@pytest.mark.asyncio
async def test_no_retry_when_config_zero():
    """SUB_AGENT_API_RETRY=0 → 不重试, 直接返回提示"""
    agent = _make_sub_agent()

    call_count = {"n": 0}

    async def _fake_create(**kwargs):
        call_count["n"] += 1
        await asyncio.sleep(1.0)

    agent._client.chat.completions.create = AsyncMock(side_effect=_fake_create)

    with patch("config.SUB_AGENT_API_TIMEOUT", 0.2), \
         patch("config.SUB_AGENT_API_RETRY", 0):
        result = await agent._call_llm_one_round(
            working=[{"role": "user", "content": "hi"}],
            tools=None,
            remaining=120,
            round_idx=0,
        )

    assert "思考时间太长了" in result
    assert call_count["n"] == 1  # 只调用 1 次, 没有重试


@pytest.mark.asyncio
async def test_remaining_too_low_skips_retry():
    """remaining < 5 时跳过重试, 直接返回提示"""
    agent = _make_sub_agent()

    # remaining=3 (< 5), 应直接返回提示, 不调用 LLM
    agent._client.chat.completions.create = AsyncMock()

    result = await agent._call_llm_one_round(
        working=[{"role": "user", "content": "hi"}],
        tools=None,
        remaining=3,
        round_idx=0,
    )

    assert "思考时间太长了" in result
    assert not agent._client.chat.completions.create.called


def test_config_values_loaded():
    """验证配置项被正确加载"""
    import config
    assert hasattr(config, "SUB_AGENT_API_TIMEOUT")
    assert hasattr(config, "SUB_AGENT_TOTAL_TIMEOUT")
    assert hasattr(config, "SUB_AGENT_API_RETRY")
    assert config.SUB_AGENT_API_TIMEOUT == 60
    assert config.SUB_AGENT_TOTAL_TIMEOUT == 150
    assert config.SUB_AGENT_API_RETRY == 1


@pytest.mark.asyncio
async def test_first_call_success_no_retry():
    """首次成功 → 不重试, attempt=0"""
    agent = _make_sub_agent()
    fake_resp = _fake_response()

    call_count = {"n": 0}

    async def _fake_create(**kwargs):
        call_count["n"] += 1
        return fake_resp

    agent._client.chat.completions.create = AsyncMock(side_effect=_fake_create)

    result = await agent._call_llm_one_round(
        working=[{"role": "user", "content": "hi"}],
        tools=None,
        remaining=120,
        round_idx=0,
    )

    assert result is fake_resp
    assert call_count["n"] == 1  # 只调用 1 次


@pytest.mark.asyncio
async def test_negative_retry_count_returns_prompt():
    """SUB_AGENT_API_RETRY=-1 (负数误配置) → 规范化为0, 不崩溃

    回归测试: 确保负数 retry_count 不会导致函数隐式返回 None
    (max(retry_count, 0) 将负数规范化为0, range(0+1) 执行一次首次调用)
    """
    agent = _make_sub_agent()
    fake_resp = _fake_response()

    call_count = {"n": 0}

    async def _fake_create(**kwargs):
        call_count["n"] += 1
        return fake_resp

    agent._client.chat.completions.create = AsyncMock(side_effect=_fake_create)

    with patch("config.SUB_AGENT_API_RETRY", -1):
        result = await agent._call_llm_one_round(
            working=[{"role": "user", "content": "hi"}],
            tools=None,
            remaining=120,
            round_idx=0,
        )

    # 关键断言: 不返回 None (原 bug 会导致 None)
    assert result is not None
    # 负数被 max(-1, 0)=0 规范化, 首次调用仍执行
    assert call_count["n"] == 1
    # 首次调用成功, 返回响应对象
    assert result is fake_resp
