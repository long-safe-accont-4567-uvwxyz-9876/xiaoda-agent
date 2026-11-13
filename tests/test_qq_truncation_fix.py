"""QQ 截断事件（2026-11-13）修复回归：
1) agnes-2.5-flash 不再判为推理模型，子代理恢复原生 tools 路径；
2) length 截断续写补救失败时，回复尾部追加可见标记，不再静默半截。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def test_agnes_not_reasoning_by_default():
    from config_providers import get_provider_capability
    assert get_provider_capability("agnes", "reasoning_by_default") is False


def test_subagent_agnes_keeps_native_tools():
    from agent_dispatcher import SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(
        name="xiaolang", display_name="小浪",
        provider="agnes", model="agnes-2.5-flash",
    )
    assert agent._is_reasoning_model() is False

    tools = [{"type": "function", "function": {"name": "calculator",
              "parameters": {"type": "object", "properties": {}}}}]
    working = [{"role": "system", "content": "系统提示"}]
    kept = agent._inject_dsml_if_needed(working, tools, agent._is_reasoning_model(), ["calculator"])
    assert kept is tools, "agnes-2.5-flash 应保留原生 tools，不再置空改走 DSML"
    assert "DSML" not in working[0]["content"], "系统提示不应被注入 DSML 工具格式说明"


def _make_router_with_continuation(continuation_content, finish_reason):
    from model_router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)

    async def _fake_continuation(*args, **kwargs):
        if continuation_content is None:
            return None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=continuation_content, tool_calls=None),
            finish_reason=finish_reason,
        )])

    router._route_for_continuation = _fake_continuation
    return router


_TRUNCATED = "让我用数学工具来解这个问题。首先我们设 sinθ = x，" + "推导过程" * 200


@pytest.mark.asyncio
async def test_discarded_continuation_gets_tail_marker():
    """续写被合并启发式丢弃（2026-11-13 事故形态）：保留截断原文且尾部加标记。"""
    tail_repeat = "推导过程" * 12  # 原文尾部区域的重复子串（>=20 字符）→ merge discarded
    router = _make_router_with_continuation(tail_repeat, "stop")
    assert tail_repeat in _TRUNCATED

    out = await router._retry_truncated_content(
        _TRUNCATED, "agnes-2.5-flash", "chat",
        [{"role": "user", "content": "求最大值"}], 0.9, 3072, "u1", "s1")

    assert out.startswith(_TRUNCATED)
    assert out.endswith("（回复被截断，可让我继续）")


@pytest.mark.asyncio
async def test_successful_continuation_has_no_tail_marker():
    """prefill 续写拼接成功且 finish=stop：不加标记。"""
    router = _make_router_with_continuation("，因此最大值为 1-a。", "stop")

    out = await router._retry_truncated_content(
        _TRUNCATED, "agnes-2.5-flash", "chat",
        [{"role": "user", "content": "求最大值"}], 0.9, 3072, "u1", "s1")

    assert "（回复被截断，可让我继续）" not in out
    assert out.endswith("，因此最大值为 1-a。")


@pytest.mark.asyncio
async def test_no_continuation_available_gets_tail_marker():
    """续写通道拿不到内容（返回 None）：截断原文尾部加标记。"""
    router = _make_router_with_continuation(None, None)

    out = await router._retry_truncated_content(
        _TRUNCATED, "agnes-2.5-flash", "chat",
        [{"role": "user", "content": "求最大值"}], 0.9, 3072, "u1", "s1")

    assert out.startswith(_TRUNCATED)
    assert out.endswith("（回复被截断，可让我继续）")
