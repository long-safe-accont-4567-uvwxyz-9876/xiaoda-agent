"""detect_emotion 否定词误判修复测试

测试覆盖:
- "不开心"/"不高兴" 等否定组合不应误判为"喜悦"（应判为悲伤/负面）
- "不难过" 中 "难过" 被否定，不应判为喜悦（也不应判为悲伤）
- "开心"/"高兴"/"难过" 等基础情绪词判定不受影响
- 多种否定前缀（不/没/别）均生效
"""
import pytest

from emotion.emotion_simple import detect_emotion


# ── 否定组合不应误判为喜悦 ──────────────────────────────────


class TestNegationNotMisjudgedAsHappy:
    """否定组合不应被误判为喜悦"""

    def test_bukai_xin_is_sad(self):
        result = detect_emotion("不开心")
        assert result["primary"] == "悲伤"
        assert result["valence"] == "negative"

    def test_bugao_xing_is_sad(self):
        result = detect_emotion("不高兴")
        assert result["primary"] == "悲伤"
        assert result["valence"] == "negative"

    def test_bu_nan_guo_not_happy(self):
        """"不难过" 中 "难过" 被否定，不应判为喜悦（也不应判为悲伤）"""
        result = detect_emotion("不难过")
        assert result["primary"] != "喜悦"
        assert result["primary"] != "悲伤"

    def test_mei_kai_xin_not_happy(self):
        result = detect_emotion("没开心")
        assert result["primary"] != "喜悦"

    def test_bie_nan_guo_not_sad(self):
        """安慰语"别难过"中"难过"被否定，不应判为悲伤"""
        result = detect_emotion("别难过")
        assert result["primary"] != "悲伤"


# ── 基础情绪词判定不受影响 ──────────────────────────────────


class TestPositiveKeywordsUnaffected:
    """基础情绪词判定不受否定处理影响"""

    def test_kai_xin_still_happy(self):
        assert detect_emotion("开心")["primary"] == "喜悦"

    def test_gao_xing_still_happy(self):
        assert detect_emotion("高兴")["primary"] == "喜悦"

    def test_nan_guo_still_sad(self):
        assert detect_emotion("难过")["primary"] == "悲伤"

    def test_hao_kai_xin_still_happy(self):
        """"好开心" 中 "开心" 前缀是 "好" 而非否定，仍为喜悦"""
        assert detect_emotion("好开心")["primary"] == "喜悦"


# ── 「别」作为词内字符（特别/离别/分别）不应被误判为否定 ──────


class TestBieSubstringMisjudgment:
    """单字否定「别」不得命中「特别」等词内部的「别」"""

    def test_tebie_kai_xin_is_happy(self):
        result = detect_emotion("特别开心")
        assert result["primary"] == "喜悦"
        assert result["primary"] != "悲伤"
        assert result["primary"] != "平静"

    def test_tebie_nan_guo_is_sad(self):
        result = detect_emotion("特别难过")
        assert result["primary"] == "悲伤"
