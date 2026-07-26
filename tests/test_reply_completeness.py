"""is_reply_likely_complete 单元测试

背景（CodeRabbit #3 复审）：
原 is_reply_likely_complete 规则 4 对 finish_reason=None 的长回复
无条件信任（return True），导致 message_processor.py 的
verification_no_finish_retry 续写重试路径变成死代码——
provider 中途关闭连接造成的真实截断永远无法被续写修复。

修复策略（优先保证功能性）：
1. finish_reason=None + 长回复 + 合法结尾 → 信任（规则 2 已覆盖）
2. finish_reason=None + 超长回复(>=200字符) + 无合法结尾 → 信任
   （避免对 agnes 风格不以标点结尾的正常长回复误判为截断）
3. finish_reason=None + 30-200字符 + 无合法结尾 → 不信任
   让调用方触发 no_finish_retry 续写重试验证完整性
   重试空/重复时由调用方标记为完整（LLM 确认完成），避免 force_close

覆盖分支：
- finish_reason="stop" + 长回复 → True（规则 1）
- 合法结尾（标点/emoji/sticker）→ True（规则 2）
- 短回复 + 无标点 → False（规则 3）
- finish_reason=None + 超长回复(>=200) + 无标点 → True（避免误判）
- finish_reason=None + 中长回复(30-200) + 无标点 → False（触发重试）
- finish_reason="length" + 长回复 + 无标点 → False（触发续写）
- 空回复 → False
"""
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

from utils.text_utils import is_reply_likely_complete


class TestReplyLikelyComplete:
    """验证 is_reply_likely_complete 的完整性判定逻辑。"""

    # ── 规则 1：finish_reason="stop" + 长回复 → 信任 LLM ──

    def test_stop_with_long_reply_trusts(self):
        """finish_reason="stop" + 长回复(>=30) → True（信任 LLM 风格选择）"""
        reply = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧"  # 22 字符 < 30
        assert is_reply_likely_complete(reply, "stop") is False  # 太短

        long_reply = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛"  # 28 字符
        # 凑到 >=30
        long_reply = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛，你说呢"  # 32 字符
        assert is_reply_likely_complete(long_reply, "stop") is True

    # ── 规则 2：合法结尾 → True ──

    def test_punctuation_ending_is_complete(self):
        """以标点结尾 → True"""
        assert is_reply_likely_complete("好的，我知道了。", None) is True
        assert is_reply_likely_complete("真的吗？", None) is True
        assert is_reply_likely_complete("太好啦！", None) is True

    def test_emoji_ending_is_complete(self):
        """以 emoji 结尾 → True（P0 修复：避免 "💕。" 丑陋输出）"""
        assert is_reply_likely_complete("嗯…让我想想哦💕", None) is True
        assert is_reply_likely_complete("太好啦✨", None) is True

    def test_sticker_tag_ending_is_complete(self):
        """以 [sticker:xxx] 结尾 → True（P0 修复：避免 "[sticker:xxx]。"）"""
        assert is_reply_likely_complete("好的，我知道了[sticker:smile]", None) is True
        assert is_reply_likely_complete("[emotion:happy]今天真开心", None) is False  # 标签不在末尾
        assert is_reply_likely_complete("今天真开心[emotion:happy]", None) is True

    # ── 规则 3：短回复 + 无标点 → False ──

    def test_short_reply_no_punctuation_incomplete(self):
        """短回复(<30字符) + 无标点 → False（可能是开场白"让我查一下"）"""
        assert is_reply_likely_complete("让我查一下", None) is False
        assert is_reply_likely_complete("嗯…让我想想哦", None) is False

    # ── 规则 4：CodeRabbit #3 修复 —— finish_reason=None 检查结尾合法性 ──

    def test_none_finish_reason_short_to_medium_reply_no_ending_returns_false(self):
        """CodeRabbit #3 核心：finish_reason=None + 30-200字符 + 无合法结尾 → False

        根因：原实现无条件 return True，导致 message_processor.py 的
        verification_no_finish_retry 续写重试路径变成死代码，
        provider 中途断连造成的真实截断永远无法被修复。

        修复：返回 False，让调用方触发 no_finish_retry 续写重试。
        重试空/重复时由调用方标记为完整（保留功能性）。
        """
        # 30-200 字符的无标点结尾回复（模拟 provider 断连截断）
        medium_reply_truncated = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛，你说呢，我等你回答"
        assert 30 <= len(medium_reply_truncated) < 200
        assert is_reply_likely_complete(medium_reply_truncated, None) is False

    def test_none_finish_reason_very_long_reply_no_ending_returns_true(self):
        """CodeRabbit #3 平衡：finish_reason=None + 超长回复(>=200字符) + 无结尾 → True

        优先保证功能性：agnes 风格不以标点结尾是正常的，
        超长回复即便无标点也信任 LLM，避免对正常长回复误判为截断。
        """
        # 200+ 字符的无标点结尾回复（agnes 风格的长回复）
        very_long_reply = (
            "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛，"
            "你说呢，我等你回答，我们可以去公园散步，或者去咖啡馆坐坐，"
            "你觉得怎么样，我有好多话想跟你说，关于最近发生的事情，"
            "还有我想到的一些有趣的想法，希望你能听完，"
            "其实我最近一直在思考一个问题，就是关于我们之间的关系，"
            "我觉得我们需要更多的沟通，更多的理解，更多的包容，"
            "你说对吗，我希望我们能一直这样下去，永远不变，"
            "我真的很珍惜我们之间的每一刻时光，希望未来也能一直陪伴着你"
        )
        assert len(very_long_reply) >= 200, f"测试文本长度: {len(very_long_reply)}"
        # 验证：不以合法结尾
        from utils.text_utils import ends_with_valid_ending
        assert ends_with_valid_ending(very_long_reply) is False
        # 验证：超长回复仍信任（保留功能性）
        assert is_reply_likely_complete(very_long_reply, None) is True

    def test_none_finish_reason_with_valid_ending_returns_true(self):
        """finish_reason=None + 合法结尾 → True（规则 2 优先于规则 4）"""
        reply = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛，你说呢💕"
        assert is_reply_likely_complete(reply, None) is True

    # ── 规则 0：finish_reason="length" 始终不完整（CodeRabbit #3 补充）──

    def test_length_with_valid_ending_still_incomplete(self):
        """CodeRabbit #3 规则 0：finish_reason="length" + 合法结尾 → 仍不完整

        根因：length 表示 LLM 因 token 上限截断，即使恰好以标点结尾
        也需要续写恢复（否则后半段内容被静默丢失）。
        优先保证功能性：截断必须触发续写重试。
        """
        assert is_reply_likely_complete("这是一段回复内容。", "length") is False
        assert is_reply_likely_complete("这是一段回复内容！", "length") is False
        assert is_reply_likely_complete("这是一段回复内容？", "length") is False

    def test_length_with_emoji_ending_still_incomplete(self):
        """finish_reason="length" + emoji 结尾 → 仍不完整"""
        assert is_reply_likely_complete("这是一段回复内容💕", "length") is False

    # ── 规则 4 兜底：finish_reason="length" → False（触发续写）──

    def test_length_finish_reason_long_reply_no_ending_returns_false(self):
        """finish_reason="length" + 长回复 + 无结尾 → False（触发续写重试）"""
        reply = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛，你说呢"
        assert is_reply_likely_complete(reply, "length") is False

    # ── 边界情况 ──

    def test_empty_reply_returns_false(self):
        """空回复 → False"""
        assert is_reply_likely_complete("", None) is False
        assert is_reply_likely_complete("   ", None) is False
        assert is_reply_likely_complete(None, None) is False

    def test_other_finish_reasons_fall_through(self):
        """finish_reason="content_filter" 等其他值 → 走规则 4 兜底"""
        # content_filter + 中长回复 + 无结尾 → False（让调用方处理）
        reply = "嗯…让我想想哦，今天天气真好，我们一起出去玩吧，好不好嘛，你说呢"
        assert is_reply_likely_complete(reply, "content_filter") is False
