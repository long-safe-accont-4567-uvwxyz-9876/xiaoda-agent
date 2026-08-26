"""中文文本处理修复测试 (Bug 6: 分词; Bug 2: 去重归一化)

覆盖:
- Bug 6: _tokenize 保留单字中文 token, 过滤单字 ASCII token
- Bug 2: _normalize_for_dedupe 不再移除所有空白, 改为合并空白
"""
import sys
from pathlib import Path

import pytest

# 确保项目根在 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.learning_feedback import _tokenize
from memory.memory_manager import _normalize_for_dedupe

# ============================================================
# Bug 6: 中文分词 - 单字中文 token 应保留
# ============================================================

class TestTokenizeChinese:
    def test_chinese_single_char_token_kept(self):
        """带空格的中文: 单字 '我' 不应被 len<2 过滤; 中文按单字保留"""
        result = _tokenize("我 喜欢 学习")
        # 单字中文不应被 len<2 过滤 (核心 bug 6 修复点)
        assert "我" in result, f"单字中文 '我' 应保留, 实际: {result}"
        # 中文按单字拆分保留 (任务允许的两种结果之一)
        for ch in "我喜欢学习":
            assert ch in result, f"中文字符 '{ch}' 应保留, 实际: {result}"

    def test_chinese_no_spaces_split_to_chars(self):
        """无空格中文应拆分为单字 token, 使其可被检索"""
        result = _tokenize("我喜欢学习")
        # 应至少包含各个中文字符
        for ch in "我喜欢学习":
            assert ch in result, f"中文字符 '{ch}' 应作为独立 token, 实际: {result}"

    def test_english_single_char_filtered(self):
        """单字 ASCII token 仍应被 len<2 过滤"""
        result = _tokenize("a b c")
        assert result == set(), f"单字 ASCII 应被过滤, 实际: {result}"

    def test_english_multichar_kept(self):
        """多字符英文 token 仍应保留"""
        result = _tokenize("hello world")
        assert "hello" in result
        assert "world" in result

    def test_chinese_not_empty(self):
        """中文文本分词结果不应为空"""
        result = _tokenize("我喜欢学习")
        assert len(result) > 0, "中文文本分词结果不应为空"


# ============================================================
# Bug 2: 去重归一化 - 不再移除所有空白
# ============================================================

class TestNormalizeForDedupeChinese:
    def test_chinese_with_spaces_not_equal_to_no_spaces(self):
        """'我喜欢学习' 与 '我 喜欢 学 习' 不应判为重复"""
        a = _normalize_for_dedupe("我喜欢学习")
        b = _normalize_for_dedupe("我 喜欢 学 习")
        assert a != b, (
            f"带空格与不带空格的中文不应判为重复: {a!r} == {b!r}"
        )

    def test_whitespace_collapsed_to_single_space(self):
        """多个空白应合并为单个空格"""
        result = _normalize_for_dedupe("hello   world")
        assert result == "hello world", f"多空格应合并为单空格, 实际: {result!r}"

    def test_leading_trailing_whitespace_trimmed(self):
        """首尾空白应被去除"""
        result = _normalize_for_dedupe("  hello world  ")
        assert result == "hello world", f"首尾空白应去除, 实际: {result!r}"

    def test_casefold_still_applied(self):
        """大小写归一化仍应生效"""
        result = _normalize_for_dedupe("Hello World")
        assert result == "hello world", f"应小写化, 实际: {result!r}"

    def test_identical_text_still_equal(self):
        """完全相同的文本仍应判为相等"""
        assert _normalize_for_dedupe("我喜欢学习") == _normalize_for_dedupe("我喜欢学习")

    def test_newlines_collapsed(self):
        """换行等空白也应合并为单空格"""
        result = _normalize_for_dedupe("hello\nworld\tfoo")
        assert result == "hello world foo", f"换行/制表应合并为单空格, 实际: {result!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
