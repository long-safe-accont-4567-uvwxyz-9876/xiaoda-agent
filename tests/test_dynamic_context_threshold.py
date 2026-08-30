"""测试动态上下文压缩阈值（修复 1）。

验证 AgentContext._get_dynamic_max_tokens() 根据当前 router 的 max_tokens
动态计算 history 阈值，而不是硬编码 200000。
"""
from __future__ import annotations

import asyncio

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


def test_threshold_small_context_8k_no_floor():
    """回归（审计 Fix2）：8K 小窗口模型阈值应严格按 70% 计算（≈5734），不被 60K 下限抬高。

    旧实现 max(history_budget, 60000) 使 8K/32K 模型阈值恒为 60K，历史永不裁剪。
    """
    ctx = AgentContext(router=_MockRouter(max_tokens=8192))
    threshold = ctx._get_dynamic_max_tokens()
    # 8192 * 0.7 = 5734.4 → int → 5734
    assert threshold == 5734, f"8K 模型阈值应≈5734，实际: {threshold}"


def test_threshold_32k_no_floor():
    """回归（审计 Fix2）：32K 模型阈值 22937（int(32768*0.7)），不与 60000 取 max。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=32768))
    assert ctx._get_dynamic_max_tokens() == 22937


def test_threshold_zero_capacity_falls_back():
    """router 上报容量 <=0 时视为未知，回退 60000 兜底。"""
    ctx = AgentContext(router=_MockRouter(max_tokens=0))
    assert ctx._get_dynamic_max_tokens() == 60000
    ctx_neg = AgentContext(router=_MockRouter(max_tokens=-5))
    assert ctx_neg._get_dynamic_max_tokens() == 60000


def test_threshold_router_exception_falls_back():
    """router.get_active_max_tokens 抛异常时回退 60000 兜底。"""

    class _BoomRouter:
        def get_active_max_tokens(self) -> int:
            raise RuntimeError("boom")

    ctx = AgentContext(router=_BoomRouter())
    assert ctx._get_dynamic_max_tokens() == 60000


def test_small_context_history_trimmed_with_20k_tokens():
    """回归（审计 Fix2）：8K 模型下 20K token 的历史必须被裁剪。

    修复前阈值被 60K 下限抬高，20K 历史 ≤ 60K → 永不触发裁剪。
    """
    ctx = AgentContext(router=_MockRouter(max_tokens=8192))
    # 中文按 1.5 系数估算：14000 字 ≈ 21000 tokens > 5734
    big_message = "历史" * 7000
    ctx.history.append({"role": "user", "content": big_message})
    before = ctx._history_tokens()
    assert before > 5734, f"测试数据未超过小上下文阈值: {before}"

    asyncio.run(ctx.add_message("assistant", "新回复"))

    # 单条超限消息无法语义压缩 → 走最终强制裁剪，移入压缩前暂存区
    assert ctx._history_tokens() <= 5734, (
        f"8K 模型下 20K 历史未被裁剪: {ctx._history_tokens()}"
    )
    assert len(ctx._pre_compressed_buffer) > 0, "被裁剪消息应进入 pre_compressed_buffer"


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
