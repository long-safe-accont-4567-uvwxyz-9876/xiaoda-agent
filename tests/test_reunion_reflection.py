"""重聚反思模块测试 — generate_reunion_message

测试覆盖:
- 短离(<30min)：简单欢迎，包含 address_term
- 中离(30min-4h)：无 router 时模板，提及"刚才聊到哪了"
- 长离(>4h)：无 router 时模板，提及"等了好久"和"今天过得怎么样"
- 离开前情绪低落：模板提及"之前说的那件事"
- _format_idle：分钟/小时/天格式化
- LLM 生成：mock router 返回字符串
- LLM 超时：降级到模板
- 带记忆和画像：不导致异常
"""
import asyncio

import pytest

from emotion.reunion_reflection import generate_reunion_message, _format_idle
from memory.emotional_memory import EmotionalMemory


class MockRouter:
    """模拟模型路由器"""
    def __init__(self, response_text="回来啦～好想你", delay=0):
        self._response = response_text
        self._delay = delay

    async def route(self, route_name, messages, temperature):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._response


# ── 短离 ──


async def test_short_idle_simple_welcome():
    """短离(<30min)：返回简单欢迎，包含 address_term"""
    msg = await generate_reunion_message(
        idle_seconds=600,  # 10 分钟
        last_emotion=("neutral", 0.0),
        address_term="爸爸",
    )
    assert isinstance(msg, str)
    assert "爸爸" in msg


async def test_short_idle_custom_address_term():
    """短离：使用自定义称呼"""
    msg = await generate_reunion_message(
        idle_seconds=300,
        last_emotion=("neutral", 0.0),
        address_term="主人",
    )
    assert "主人" in msg


# ── 中离 ──


async def test_medium_idle_template():
    """中离(30min-4h)：无 router 时返回模板消息，提及"刚才聊到哪了" """
    msg = await generate_reunion_message(
        idle_seconds=3600 * 2,  # 2 小时
        last_emotion=("neutral", 0.0),
    )
    assert isinstance(msg, str)
    assert "刚才聊到哪了" in msg


# ── 长离 ──


async def test_long_idle_template():
    """长离(>4h)：无 router 时返回模板消息，提及"等了好久"和"今天过得怎么样" """
    msg = await generate_reunion_message(
        idle_seconds=3600 * 6,  # 6 小时
        last_emotion=("neutral", 0.0),
    )
    assert isinstance(msg, str)
    assert "等了好久" in msg
    assert "今天过得怎么样" in msg


# ── 离开前情绪低落 ──


async def test_low_emotion_template():
    """离开前情绪低落：返回模板消息，提及"之前说的那件事" """
    msg = await generate_reunion_message(
        idle_seconds=3600 * 2,  # 2 小时
        last_emotion=("悲伤", 0.7),
    )
    assert isinstance(msg, str)
    assert "之前说的那件事" in msg


# ── _format_idle ──


def test_format_idle_minutes():
    """_format_idle：分钟格式化"""
    assert _format_idle(120) == "2分钟"
    assert _format_idle(1800) == "30分钟"


def test_format_idle_hours():
    """_format_idle：小时格式化"""
    assert _format_idle(3600) == "1小时"
    assert _format_idle(7200) == "2小时"


def test_format_idle_days():
    """_format_idle：天格式化"""
    assert _format_idle(86400) == "1天"
    assert _format_idle(86400 * 3) == "3天"


# ── LLM 生成 ──


async def test_llm_generation_with_mock_router():
    """LLM 生成：有 mock router 时返回 LLM 文本"""
    router = MockRouter(response_text="回来啦～好想你呀")
    msg = await generate_reunion_message(
        idle_seconds=3600 * 2,
        last_emotion=("neutral", 0.0),
        router=router,
    )
    assert msg == "回来啦～好想你呀"


# ── LLM 超时 ──


async def test_llm_timeout_falls_back_to_template(monkeypatch):
    """LLM 超时：mock router 延迟超过 timeout 时降级到模板

    实现内部用 asyncio.wait_for(..., timeout=10)。
    为避免真实等待 10s，这里将 wait_for 的 timeout 缩短到 0.05s，
    mock router 延迟 0.3s > 0.05s → 超时 → 降级到模板。
    """
    real_wait_for = asyncio.wait_for

    async def quick_wait_for(coro, timeout):
        return await real_wait_for(coro, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", quick_wait_for)

    router = MockRouter(delay=0.3)  # 延迟 0.3s > 0.05s timeout
    msg = await generate_reunion_message(
        idle_seconds=3600 * 2,  # 中离
        last_emotion=("neutral", 0.0),
        router=router,
    )
    assert isinstance(msg, str)
    # 降级到中离模板：提及"刚才聊到哪了"
    assert "刚才聊到哪了" in msg


# ── 带记忆和画像 ──


async def test_with_memories_and_portrait_no_crash():
    """带记忆和画像：emotional_memories 和 portrait 不导致异常"""
    memories = [
        EmotionalMemory(
            id="em_1", user_id="u1", event="加班", emotion="焦虑",
            context="最近加班好多", keywords=["加班"],
        ),
        EmotionalMemory(
            id="em_2", user_id="u1", event="升职", emotion="喜悦",
            context="终于升职了", keywords=["升职"],
        ),
    ]
    portrait = {"interests": ["编程", "音乐", "电影", "游戏"]}

    # 无 router 也不崩溃
    msg = await generate_reunion_message(
        idle_seconds=3600 * 5,
        last_emotion=("neutral", 0.0),
        emotional_memories=memories,
        portrait=portrait,
    )
    assert isinstance(msg, str)

    # 有 router 也不崩溃
    router = MockRouter(response_text="回来啦～")
    msg2 = await generate_reunion_message(
        idle_seconds=3600 * 5,
        last_emotion=("悲伤", 0.6),
        emotional_memories=memories,
        portrait=portrait,
        router=router,
    )
    assert isinstance(msg2, str)
