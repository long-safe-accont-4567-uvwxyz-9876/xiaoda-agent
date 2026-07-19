"""测试动态上下文压缩阈值（修复 1）。

验证 AgentContext._get_dynamic_max_tokens() 根据当前 router 的 max_tokens
动态计算 history 阈值，而不是硬编码 200000。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_context import AgentContext


class _MockRouter:
    """Mock router，可配置返回的 max_tokens。"""

    def __init__(self, max_tokens: int) -> None:
        self._max_tokens = max_tokens

    def get_active_max_tokens(self) -> int:
        return self._max_tokens


class _NoMethodRouter:
    """模拟旧版 router，没有 get_active_max_tokens 方法。"""


def test_threshold_mimo_128k():
    """mimo chat (128K) 模式下阈值应约 90K（70% of 131072）。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=131072))
    threshold = ctx._get_dynamic_max_tokens()
    # 131072 * 0.7 = 91750.4
    assert 85000 <= threshold <= 95000, f"mimo 128K 阈值异常: {threshold}"


def test_threshold_chat_ultra_1m():
    """chat_ultra (1M) 模式下阈值应约 730K（70% of 1048576）。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=1048576))
    threshold = ctx._get_dynamic_max_tokens()
    # 1048576 * 0.7 = 734003.2
    assert 700000 <= threshold <= 800000, f"chat_ultra 1M 阈值异常: {threshold}"


def test_threshold_chat_flash_6k():
    """chat_flash (6K) 模式下阈值会受 FALLBACK_MAX_HISTORY_TOKENS 兜底保护。

    chat_flash 实际 max_tokens=6144，70% 仅 4300，但兜底值 60000 会顶上。
    这避免了极端小窗口导致过度压缩。
    """
    ctx = AgentContext(router=_MockRouter(max_tokens=6144))
    threshold = ctx._get_dynamic_max_tokens()
    # 应不低于 FALLBACK_MAX_HISTORY_TOKENS=60000
    assert threshold == 60000, f"chat_flash 阈值应受兜底保护: {threshold}"


def test_threshold_no_router_fallback():
    """router 为 None 时回退到 FALLBACK_MAX_HISTORY_TOKENS=60000。"""
    ctx = AgentContext(router=None)
    threshold = ctx._get_dynamic_max_tokens()
    assert threshold == 60000, f"无 router 兜底失败: {threshold}"


def test_threshold_old_router_no_method():
    """旧版 router 没有 get_active_max_tokens 方法时回退到兜底。"""
    ctx = AgentContext(router=_NoMethodRouter())
    threshold = ctx._get_dynamic_max_tokens()
    assert threshold == 60000, f"旧版 router 兜底失败: {threshold}"


def test_keep_recent_small_context():
    """小上下文（<512K）保留 5 轮。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=131072))  # 128K < 512K
    assert ctx._get_keep_recent() == 5


def test_keep_recent_large_context():
    """大上下文（≥512K）保留 10 轮。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=1048576))  # 1M ≥ 512K
    assert ctx._get_keep_recent() == 10


def test_keep_recent_boundary_512k():
    """边界值：正好 512K 视为大上下文。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=524288))  # 正好 512K
    assert ctx._get_keep_recent() == 10


def test_compress_now_returns_dict_with_required_fields():
    """compress_now 返回包含必要字段的 dict。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=131072))

    # 空 history 时不压缩，返回 saved_tokens=0
    result = asyncio.run(ctx.compress_now())

    assert isinstance(result, dict)
    for key in ("before_tokens", "after_tokens", "saved_tokens",
                "before_messages", "after_messages", "rounds", "max_tokens", "message"):
        assert key in result, f"compress_now 缺少字段: {key}"


def test_compress_now_empty_history_returns_zero_saved():
    """空 history 时 saved_tokens 应为 0。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=131072))
    result = asyncio.run(ctx.compress_now())
    assert result["saved_tokens"] == 0
    assert result["before_tokens"] == 0
    assert "未超阈值" in result["message"] or "无需压缩" in result["message"]


def test_compress_now_actually_compresses_when_over_threshold():
    """history 超阈值时 compress_now 应实际压缩并节省 token。

    用极小 max_tokens 模拟超阈值（兜底 60000），加入大量历史触发压缩。
    """
    # 兜底 max_tokens=60000，加入 80 条长消息 = ~6K * 80 = 480K，远超 60K
    ctx = AgentContext(router=_NoMethodRouter())  # 阈值 60000

    # 加大量历史
    for i in range(80):
        ctx.history.append({"role": "user", "content": f"用户消息 {i} " * 100})
        ctx.history.append({"role": "assistant", "content": f"助手回复 {i} " * 100})

    before = ctx._history_tokens()
    assert before > 60000, f"测试数据未超过阈值: {before}"

    result = asyncio.run(ctx.compress_now())
    assert result["before_tokens"] == before
    assert result["after_tokens"] < before, f"压缩未生效: before={before} after={result['after_tokens']}"
    assert result["saved_tokens"] > 0
    assert result["after_messages"] < result["before_messages"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
