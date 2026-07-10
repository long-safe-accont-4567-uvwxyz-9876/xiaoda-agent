"""nudge_engine 情感记忆注入测试 (I3)

测试覆盖:
- NudgeEngine 构造：传入 mock 参数可创建实例
- _get_address_term：返回非空字符串（默认"爸爸"）
- 情感记忆注入不崩溃：mock emotional_memory 后 _generate_idle_greeting 不抛异常
- 情感记忆注入确实调用了 recall（TDD: 实现前应失败）
"""
from unittest.mock import MagicMock, patch

import pytest

from emotion.nudge_engine import NudgeEngine
from memory.emotional_memory import EmotionalMemory


class MockDB:
    async def fetch_one(self, *a, **kw):
        return {"c": 0}

    async def fetch_all(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        pass


class MockAnalytics:
    pass


class MockRouter:
    async def route(self, *a, **kw):
        return "你好呀～"


class MockAPI:
    pass


def _make_engine():
    """创建测试用 NudgeEngine 实例（DND 永不触发）"""
    return NudgeEngine(
        db=MockDB(),
        analytics=MockAnalytics(),
        router=MockRouter(),
        api=MockAPI(),
        user_openid="test_openid_123",
        dnd_start=25,  # 永不触发 DND（hour >= 25 不可能）
        dnd_end=-1,    # 永不触发 DND（hour < -1 不可能）
    )


# ── 构造 ──


def test_nudge_engine_construction():
    """nudge_engine 构造：传入 mock 参数可创建实例"""
    engine = _make_engine()
    assert engine is not None
    assert engine._user_openid == "test_openid_123"
    assert engine._router is not None


# ── _get_address_term ──


def test_get_address_term_returns_non_empty(tmp_path):
    """_get_address_term：返回非空字符串（默认"爸爸"）"""
    engine = _make_engine()
    # 指向空目录，确保 USER.md 不存在 → 返回默认"爸爸"
    with patch("config.WORKSPACE_DIR", tmp_path):
        term = engine._get_address_term()
    assert isinstance(term, str)
    assert len(term) > 0
    assert term == "爸爸"


# ── 情感记忆注入不崩溃 ──


async def test_memory_injection_no_crash():
    """情感记忆注入不崩溃：mock emotional_memory 后 _generate_idle_greeting 不抛异常"""
    engine = _make_engine()

    mock_mgr = MagicMock()
    mock_mgr.recall.return_value = []  # 空记忆列表

    with patch(
        "memory.emotional_memory.get_emotional_memory_manager",
        return_value=mock_mgr,
    ):
        result = await engine._generate_idle_greeting(idle_seconds=14400)

    assert isinstance(result, str)


async def test_memory_injection_with_memories_no_crash():
    """情感记忆注入（有记忆）不崩溃"""
    engine = _make_engine()

    mock_mem = EmotionalMemory(
        id="em_1",
        user_id="qq_test_openid_123",
        event="工作压力",
        emotion="焦虑",
        context="最近工作压力大",
        keywords=["工作", "压力"],
    )
    mock_mgr = MagicMock()
    mock_mgr.recall.return_value = [mock_mem]

    with patch(
        "memory.emotional_memory.get_emotional_memory_manager",
        return_value=mock_mgr,
    ):
        result = await engine._generate_idle_greeting(idle_seconds=14400)

    assert isinstance(result, str)


# ── 情感记忆注入确实调用了 recall（TDD: 实现前应失败）──


async def test_memory_injection_calls_recall():
    """情感记忆注入：_generate_idle_greeting 调用 emotional_memory.recall"""
    engine = _make_engine()

    mock_mgr = MagicMock()
    mock_mgr.recall.return_value = []

    with patch(
        "memory.emotional_memory.get_emotional_memory_manager",
        return_value=mock_mgr,
    ) as mock_get:
        await engine._generate_idle_greeting(idle_seconds=14400)

    # 验证调用了 get_emotional_memory_manager
    mock_get.assert_called_once()
    # 验证调用了 recall，且 user_id 为 qq_{openid}
    mock_mgr.recall.assert_called_once()
    call_args = mock_mgr.recall.call_args
    user_id_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("user_id")
    assert user_id_arg == "qq_test_openid_123"
