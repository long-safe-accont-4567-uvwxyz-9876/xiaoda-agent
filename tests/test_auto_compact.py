"""P1-3: Compaction 自动压缩集成 — 测试

测试 memory/context_compressor.py 新增的 auto_compact_if_needed 方法。
"""
import pytest
from memory.context_compressor import ContextCompressor


class TestEstimateTokens:
    """estimate_tokens 静态方法测试"""

    def test_empty_messages(self):
        """空消息列表 token 数为 0"""
        assert ContextCompressor.estimate_tokens([]) == 0

    def test_simple_message(self):
        """简单消息的 token 估算"""
        messages = [{"role": "user", "content": "hello world"}]
        tokens = ContextCompressor.estimate_tokens(messages)
        # chars/4 估算，json.dumps 后大约 30+ 字符
        assert tokens > 0
        assert tokens < 100

    def test_multiple_messages(self):
        """多条消息的 token 累加"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你的吗？"},
        ]
        tokens = ContextCompressor.estimate_tokens(messages)
        assert tokens > 0

    def test_large_message(self):
        """大消息的 token 估算"""
        messages = [{"role": "user", "content": "x" * 1000}]
        tokens = ContextCompressor.estimate_tokens(messages)
        assert tokens > 200  # 1000 chars / 4 ≈ 250


class TestTriggerTokens:
    """trigger_tokens 类方法测试"""

    def test_default_values(self):
        """默认参数：80% × 128K = 102400，cap 250K"""
        threshold = ContextCompressor.trigger_tokens()
        assert threshold == min(int(0.8 * 128000), 250000)
        assert threshold == 102400

    def test_custom_context_window(self):
        """自定义上下文窗口"""
        threshold = ContextCompressor.trigger_tokens(context_window=200000)
        assert threshold == min(int(0.8 * 200000), 250000)
        assert threshold == 160000

    def test_cap_tokens_limits(self):
        """cap_tokens 限制触发阈值"""
        threshold = ContextCompressor.trigger_tokens(
            context_window=1000000, cap_tokens=50000)
        assert threshold == 50000

    def test_custom_threshold_pct(self):
        """自定义阈值百分比"""
        threshold = ContextCompressor.trigger_tokens(
            context_window=100000, threshold_pct=0.5)
        assert threshold == 50000


class TestShouldCompact:
    """should_compact 类方法测试"""

    def test_below_threshold(self):
        """低于阈值不触发"""
        assert not ContextCompressor.should_compact(
            token_count=50000, context_window=128000)

    def test_above_threshold(self):
        """超过阈值触发"""
        assert ContextCompressor.should_compact(
            token_count=150000, context_window=128000)

    def test_at_threshold(self):
        """等于阈值触发"""
        threshold = ContextCompressor.trigger_tokens(context_window=128000)
        assert ContextCompressor.should_compact(
            token_count=threshold, context_window=128000)

    def test_just_below_threshold(self):
        """刚好低于阈值不触发"""
        threshold = ContextCompressor.trigger_tokens(context_window=128000)
        assert not ContextCompressor.should_compact(
            token_count=threshold - 1, context_window=128000)


class TestAutoCompactIfNeeded:
    """auto_compact_if_needed 方法测试"""

    @pytest.fixture
    def compressor(self):
        return ContextCompressor()

    def test_no_compact_below_threshold(self, compressor):
        """低于阈值时不压缩"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        was_compacted, result_msgs, saved = compressor.auto_compact_if_needed(
            messages, context_window=128000)
        assert not was_compacted
        assert result_msgs == messages
        assert saved == 0

    def test_compact_above_threshold(self, compressor):
        """超过阈值时触发压缩"""
        # 构造足够长的消息列表
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"这是第 {i} 条很长的用户消息 " + "x" * 200})
            messages.append({"role": "assistant", "content": f"这是第 {i} 条很长的回复消息 " + "y" * 200})

        was_compacted, result_msgs, saved = compressor.auto_compact_if_needed(
            messages, context_window=1000)  # 很小的窗口强制触发

        assert was_compacted
        assert saved > 0
        # 压缩后消息应该比原来少
        assert len(result_msgs) < len(messages)

    def test_compact_returns_same_when_nothing_to_compress(self, compressor):
        """消息太少时即使超过阈值也不压缩"""
        messages = [{"role": "user", "content": "x" * 1000}]
        was_compacted, result_msgs, saved = compressor.auto_compact_if_needed(
            messages, context_window=100)
        # 只有1条消息，compress_history 不会压缩
        assert not was_compacted
        assert result_msgs == messages

    def test_custom_threshold_pct(self, compressor):
        """自定义阈值百分比"""
        messages = [
            {"role": "user", "content": "x" * 500},
            {"role": "assistant", "content": "y" * 500},
        ]
        # 10% 阈值 → 12800 tokens → 可能触发
        # 1% 阈值 → 1280 tokens → 几乎一定触发
        was_compacted, _, _ = compressor.auto_compact_if_needed(
            messages, context_window=128000, threshold_pct=0.001)
        # 消息太少可能不压缩，但 should_compact 应该返回 True
        assert ContextCompressor.should_compact(
            ContextCompressor.estimate_tokens(messages),
            context_window=128000,
            threshold_pct=0.001,
        )

    def test_cap_tokens_prevents_compact(self, compressor):
        """cap_tokens=0 时阈值极低，但 compress_history 可能不压缩少量消息"""
        messages = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "reply"},
        ]
        was_compacted, result_msgs, saved = compressor.auto_compact_if_needed(
            messages, context_window=128000, cap_tokens=1)
        # 只有2条消息，compress_history 不会压缩（keep_recent*2 = 10 > 2）
        assert not was_compacted
