"""英文推理泄漏检测和清洗的单元测试。

验证 agnes-2.0-flash 长回复截断时泄漏的英文计划/总结模式能被正确检测和清洗。
生产样本 conversation_logs id 2107（2026-07-25 记忆回忆任务）。
"""
import pytest
from utils.llm_cleanup import has_english_reasoning_leak, strip_english_reasoning_leak


# ── 生产样本（简化版） ──────────────────────────────────────────
PROD_TRUNCATED = """唔……爸爸让小妲回忆这几天发生的事情呀？好，人家乖乖翻一翻记忆的书架～📚✨

7月18日（周六）—— 温柔而绵长的清晨 🌸
这一天印象最深的是早晨！

7月20日（周一）—— 稍微平淡一点的日子 🍃
当天晚上有没有想我想得睡不着觉呀 😉 。不过老实说那天的记忆没那么鲜明啦可能是因为没什么特别的大事件发生耶～

Anyway continuing now ~~~~

(深吸一口气)
好吧接下来继续讲完 ——>

(Summary complete) -> Final Output Below."""


# ── 检测测试 ──────────────────────────────────────────────────
class TestHasEnglishReasoningLeak:
    """测试 has_english_reasoning_leak 检测函数。"""

    def test_prod_sample_detected(self):
        """生产样本应被检测为泄漏。"""
        assert has_english_reasoning_leak(PROD_TRUNCATED) is True

    def test_anyway_continuing(self):
        assert has_english_reasoning_leak("正常内容\n\nAnyway continuing now ~~~~") is True

    def test_summary_complete(self):
        assert has_english_reasoning_leak("(Summary complete) -> Final Output Below.") is True

    def test_final_output_below(self):
        assert has_english_reasoning_leak("Final Output Below.") is True

    def test_chinese_meta_instruction(self):
        """中文元指令 '继续讲完 ——>' 也应被检测。"""
        assert has_english_reasoning_leak("正常内容\n\n好吧接下来继续讲完 ——>") is True

    def test_normal_reply_not_flagged(self):
        """正常中文回复不应被误判。"""
        normal = "爸爸早上好呀～一起去公园看花听起来好浪漫！小妲已经想象到花园里五彩缤纷的花朵在阳光下绽放的样子了。你最喜欢什么花呢？小妲最喜欢向日葵啦，因为它们总是朝着太阳微笑～🌻。"
        assert has_english_reasoning_leak(normal) is False

    def test_normal_reply_with_english_not_flagged(self):
        """正常含英文单词的回复不应被误判（如 'Output' 单独出现）。"""
        normal = "今天的 output 指标不错呢。"
        assert has_english_reasoning_leak(normal) is False

    def test_empty_text(self):
        assert has_english_reasoning_leak("") is False

    def test_none_text(self):
        assert has_english_reasoning_leak(None) is False  # type: ignore[arg-type]


# ── 清洗测试 ──────────────────────────────────────────────────
class TestStripEnglishReasoningLeak:
    """测试 strip_english_reasoning_leak 清洗函数。"""

    def test_prod_sample_cleaned(self):
        """生产样本清洗后应保留正常中文内容，移除英文泄漏。"""
        cleaned = strip_english_reasoning_leak(PROD_TRUNCATED, context="test")
        assert "Anyway continuing" not in cleaned
        assert "Summary complete" not in cleaned
        assert "Final Output Below" not in cleaned
        assert "好吧接下来继续" not in cleaned
        # 正常内容应保留
        assert "7月18日" in cleaned
        assert "7月20日" in cleaned
        assert "大事件发生耶～" in cleaned

    def test_normal_reply_unchanged(self):
        """正常回复应原样返回，不做修改。"""
        normal = "爸爸早上好呀～一起去公园看花听起来好浪漫！🌻。"
        cleaned = strip_english_reasoning_leak(normal, context="test")
        assert cleaned == normal

    def test_leak_at_start(self):
        """泄漏在开头时，清洗后应为空或极短。"""
        leaked = "Anyway continuing now ~~~~\n\n正常内容"
        cleaned = strip_english_reasoning_leak(leaked, context="test")
        assert "Anyway" not in cleaned
        assert len(cleaned) < 5  # 开头就是泄漏，清洗后几乎为空

    def test_leak_in_middle(self):
        """泄漏在中间时，应从泄漏点截断，保留之前的内容。"""
        leaked = "第一段正常内容。\n\nAnyway continuing now\n\n第二段不应保留"
        cleaned = strip_english_reasoning_leak(leaked, context="test")
        assert "第一段正常内容" in cleaned
        assert "Anyway" not in cleaned
        assert "第二段" not in cleaned

    def test_multiple_leak_patterns(self):
        """多个泄漏模式同时存在时，应从最早的位置截断。"""
        leaked = "正常内容。\n\nAnyway continuing now\n\n(Summary complete) -> Final Output Below."
        cleaned = strip_english_reasoning_leak(leaked, context="test")
        assert "正常内容" in cleaned
        assert "Anyway" not in cleaned
        assert "Summary" not in cleaned

    def test_empty_text(self):
        assert strip_english_reasoning_leak("", context="test") == ""

    def test_no_leak_returns_original(self):
        """无泄漏时返回原文本（同一对象）。"""
        normal = "这是一段正常的回复。"
        cleaned = strip_english_reasoning_leak(normal, context="test")
        assert cleaned == normal

    def test_chinese_meta_instruction_only(self):
        """只有中文元指令（无英文）也应被检测和清洗。"""
        leaked = "正常内容到这里。\n\n好吧接下来继续讲完 ——>"
        cleaned = strip_english_reasoning_leak(leaked, context="test")
        assert "正常内容到这里" in cleaned
        assert "好吧接下来" not in cleaned

    def test_output_below_standalone(self):
        """ 'Output Below' 单独出现也应被检测。"""
        leaked = "内容到这里结束。\n\nOutput Below."
        cleaned = strip_english_reasoning_leak(leaked, context="test")
        assert "内容到这里结束" in cleaned
        assert "Output Below" not in cleaned


# ── 集成测试：模拟截断检测场景 ──────────────────────────────────
class TestTruncationDetectionIntegration:
    """模拟截断检测逻辑，验证英文泄漏会触发重试。"""

    def test_leak_triggers_incomplete_detection(self):
        """模拟 model_router._is_reply_incomplete 逻辑：
        英文泄漏时，即使 last_line >= 10 字符，也应判定为不完整。
        """
        content = PROD_TRUNCATED
        content_rstripped = content.rstrip()
        content_last_line = content_rstripped.split('\n')[-1] if content_rstripped else ""
        has_eng_leak = has_english_reasoning_leak(content)
        ends_with_punct = any(content_rstripped.endswith(c) for c in "。！？～…）」】\n")

        # 原规则：last_line >= 10 → 不判定为截断
        assert len(content_last_line) >= 10  # last line 很长
        assert not ends_with_punct  # 不以中文标点结尾

        # 原规则会漏判：not ends_with_punct AND len(last_line) < 10 → False
        old_rule = bool(content) and len(content) >= 30 and (
            not ends_with_punct and len(content_last_line) < 10
        )
        assert old_rule is False  # 原规则漏判！

        # 新规则：英文泄漏 → 判定为截断
        new_rule = bool(content) and len(content) >= 30 and (
            not ends_with_punct and (len(content_last_line) < 10 or has_eng_leak)
        )
        assert new_rule is True  # 新规则正确检测！
        assert has_eng_leak is True

    def test_normal_reply_not_flagged_as_incomplete(self):
        """正常回复不应被新规则误判为截断。"""
        normal = "爸爸早上好呀～一起去公园看花听起来好浪漫！🌻。"
        content_rstripped = normal.rstrip()
        content_last_line = content_rstripped.split('\n')[-1] if content_rstripped else ""
        has_eng_leak = has_english_reasoning_leak(normal)
        ends_with_punct = any(content_rstripped.endswith(c) for c in "。！？～…）」】\n")

        new_rule = bool(normal) and len(normal) >= 30 and (
            not ends_with_punct and (len(content_last_line) < 10 or has_eng_leak)
        )
        assert new_rule is False  # 正常回复不触发
        assert has_eng_leak is False
