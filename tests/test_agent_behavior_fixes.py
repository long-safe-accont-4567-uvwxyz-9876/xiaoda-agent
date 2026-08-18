"""测试 Agent 行为问题的修复 — 针对用户反馈的4个问题.

Issue 1: 时间感知不稳定 — 时间上下文需要明确指示忽略历史中的旧时间
Issue 2: 人设和任务混杂 — 工具总结prompt需要分离工具状态和人格表达
Issue 3: 上下文恢复能力弱 — 需要失败状态保存和恢复机制
Issue 4: 工具链结果表达混乱 — 总结prompt需要结构化表达约束
"""
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════
# Issue 1: 时间感知不稳定
# ═══════════════════════════════════════════════════════════════

def test_time_context_explicitly_disregards_history_time():
    """时间上下文应明确指示 LLM 忽略历史消息中的旧时间引用.

    用户反馈: 群主说现在不是1:30, agent之前却说了1:30.
    根因: 历史消息中有旧时间引用, LLM 被误导.
    修复: 时间上下文需要明确说"历史消息中的时间已过时".
    """
    from agent_context import AgentContext

    ctx = AgentContext.__new__(AgentContext)
    result = ctx._build_time_context()

    # 时间上下文必须包含明确指示: 历史时间不可信
    assert "历史" in result or "过时" in result or "不得" in result, \
        f"时间上下文应明确指示历史时间不可信, 实际: {result}"


def test_get_current_time_tool_uses_zoneinfo():
    """get_current_time 工具应使用 ZoneInfo, 与 agent_context 保持一致.

    根因: code_tools_v2.py 用 timezone(timedelta(hours=8)),
          agent_context.py 用 ZoneInfo("Asia/Shanghai").
          虽然结果相同, 但实现不一致, 且不支持 NUDGE_TIMEZONE 覆盖.
    修复: 统一使用 ZoneInfo.
    """
    import inspect
    from tools import code_tools_v2

    source = inspect.getsource(code_tools_v2.get_current_time)
    assert "ZoneInfo" in source, \
        "get_current_time 应使用 ZoneInfo 而非 timezone(timedelta(hours=8))"


def test_get_current_time_respects_nudge_timezone():
    """get_current_time 工具应支持 NUDGE_TIMEZONE 环境变量."""
    import os
    from zoneinfo import ZoneInfo
    from datetime import datetime

    old_tz = os.environ.get("NUDGE_TIMEZONE")
    try:
        os.environ["NUDGE_TIMEZONE"] = "Asia/Tokyo"
        from tools.code_tools_v2 import get_current_time
        result = get_current_time()
        tokyo_now = datetime.now(ZoneInfo("Asia/Tokyo"))
        assert f"{tokyo_now.hour:02d}:" in str(result.data) or f"{tokyo_now.hour}:" in str(result.data), \
            f"get_current_time 应支持 NUDGE_TIMEZONE 覆盖, 实际: {result.data}"
    finally:
        if old_tz is not None:
            os.environ["NUDGE_TIMEZONE"] = old_tz
        else:
            os.environ.pop("NUDGE_TIMEZONE", None)


# ═══════════════════════════════════════════════════════════════
# Issue 2 & 4: 人设/任务混杂 + 工具结果表达混乱
# ═══════════════════════════════════════════════════════════════

def test_main_agent_summarize_prompt_has_structure():
    """主Agent的 _summarize_results prompt 应包含结构化表达约束.

    用户反馈: "数据加载完毕" 到底加载了什么? 不清楚.
    根因: tool_call_handler._summarize_results 的 prompt 缺乏结构约束.
    修复: prompt 应要求先说明执行了什么操作, 再描述结果.
    """
    import inspect
    from tool_engine.tool_call_handler import ToolCallHandler

    source = inspect.getsource(ToolCallHandler._summarize_results)
    # prompt 应包含结构化指引: 说明操作 + 描述结果
    assert "说明" in source or "执行" in source or "操作" in source, \
        "summarize prompt 应要求说明执行了什么操作"
    # 不应只说"数据加载完毕"这种模糊表述
    assert "模糊" in source or "清楚" in source or "明确" in source, \
        "summarize prompt 应要求明确表达, 避免模糊表述"


def test_sub_agent_summarize_prompt_has_structure():
    """子Agent的 _summarize_after_tools prompt 应包含结构化表达约束."""
    import inspect
    from agent_dispatcher import SubAgent

    source = inspect.getsource(SubAgent._summarize_after_tools)
    # prompt 应包含结构化指引
    assert "说明" in source or "执行" in source or "操作" in source, \
        "子Agent summarize prompt 应要求说明执行了什么操作"


# ═══════════════════════════════════════════════════════════════
# Issue 3: 上下文恢复能力弱
# ═══════════════════════════════════════════════════════════════

def test_agent_context_has_record_failure_method():
    """AgentContext 应有 record_failure 方法用于记录处理失败."""
    from agent_context import AgentContext

    ctx = AgentContext.__new__(AgentContext)
    assert hasattr(ctx, 'record_failure'), \
        "AgentContext 应有 record_failure 方法"
    assert callable(getattr(ctx, 'record_failure', None)), \
        "record_failure 应是可调用方法"


def test_agent_context_has_consume_failure_method():
    """AgentContext 应有 consume_failure 方法用于读取并清除失败记录."""
    from agent_context import AgentContext

    ctx = AgentContext.__new__(AgentContext)
    assert hasattr(ctx, 'consume_failure'), \
        "AgentContext 应有 consume_failure 方法"
    assert callable(getattr(ctx, 'consume_failure', None)), \
        "consume_failure 应是可调用方法"


def test_record_and_consume_failure():
    """record_failure 应存储失败信息, consume_failure 应返回并清除."""
    from agent_context import AgentContext

    ctx = AgentContext.__new__(AgentContext)
    ctx._last_failure = None

    ctx.record_failure("timeout", "用户问了什么时间")
    failure = ctx.consume_failure()

    assert failure is not None, "consume_failure 应返回已记录的失败"
    assert failure["type"] == "timeout"
    assert "用户问了什么时间" in failure["input_preview"]

    # 再次 consume 应返回 None (已清除)
    assert ctx.consume_failure() is None, "consume_failure 应清除失败记录"


def test_failure_expires_after_5_minutes():
    """失败记录超过5分钟应自动过期."""
    from agent_context import AgentContext

    ctx = AgentContext.__new__(AgentContext)
    ctx._last_failure = None

    ctx.record_failure("timeout", "test")
    # 模拟5分钟后
    ctx._last_failure["timestamp"] = time.time() - 301

    failure = ctx.consume_failure()
    assert failure is None, "超过5分钟的失败记录应过期"


def test_volatile_content_includes_failure_reminder():
    """_build_volatile_content 应在有失败记录时注入失败提醒."""
    from agent_context import AgentContext

    ctx = AgentContext(
        system_prompt="test",
    )
    ctx.record_failure("timeout", "上次问的问题")

    content = ctx._build_volatile_content(source="")
    assert "上次" in content or "失败" in content or "超时" in content, \
        f"volatile content 应包含失败提醒, 实际: {content[:200]}"


def test_consume_failure_clears_reminder():
    """consume_failure 后, _build_volatile_content 不应再包含失败提醒."""
    from agent_context import AgentContext

    ctx = AgentContext(system_prompt="test")
    ctx.record_failure("timeout", "test input")
    ctx.consume_failure()  # 清除

    content = ctx._build_volatile_content(source="")
    assert "上次" not in content or "失败" not in content, \
        "清除失败记录后不应再包含失败提醒"
