"""detect_emotion 返回 pad 字段的扩展测试

测试覆盖:
- 空文本/无关键词 → pad 为 neutral 值
- 正面情绪文本 → pad.P > 0
- 负面情绪文本 → pad.P < 0
- 旧字段 primary/valence/intensity 仍然存在且正确（向下兼容）
"""
import pytest

from emotion.emotion_simple import detect_emotion
from emotion.pad_model import PADEmotion


# ── pad 字段存在性 ──────────────────────────────────────────


class TestDetectEmotionPadField:
    """detect_emotion 返回值包含 pad 字段"""

    def test_pad_key_present_on_neutral(self):
        result = detect_emotion("")
        assert "pad" in result

    def test_pad_key_present_on_emotion(self):
        result = detect_emotion("好开心")
        assert "pad" in result

    def test_pad_is_dict(self):
        result = detect_emotion("好开心")
        assert isinstance(result["pad"], dict)

    def test_pad_has_PAD_keys(self):
        result = detect_emotion("好开心")
        assert set(result["pad"].keys()) == {"P", "A", "D"}


# ── 平静/空文本 → neutral ──────────────────────────────────


class TestNeutralPad:
    """空文本/无关键词时 pad 为 neutral 值"""

    def test_empty_text_pad_neutral(self):
        result = detect_emotion("")
        neutral = PADEmotion.neutral().to_dict()
        assert result["pad"]["P"] == pytest.approx(neutral["P"])
        assert result["pad"]["A"] == pytest.approx(neutral["A"])
        assert result["pad"]["D"] == pytest.approx(neutral["D"])

    def test_whitespace_text_pad_neutral(self):
        result = detect_emotion("   ")
        neutral = PADEmotion.neutral().to_dict()
        assert result["pad"]["D"] == pytest.approx(neutral["D"])

    def test_no_keyword_text_pad_neutral(self):
        result = detect_emotion("请问现在几点了")
        neutral = PADEmotion.neutral().to_dict()
        assert result["pad"]["P"] == pytest.approx(neutral["P"])
        assert result["pad"]["A"] == pytest.approx(neutral["A"])

    def test_neutral_pad_D_is_half(self):
        """neutral 的 D=0.5"""
        result = detect_emotion("")
        assert result["pad"]["D"] == pytest.approx(0.5)

    def test_neutral_pad_P_is_zero(self):
        result = detect_emotion("")
        assert result["pad"]["P"] == pytest.approx(0.0)

    def test_neutral_pad_A_is_zero(self):
        result = detect_emotion("")
        assert result["pad"]["A"] == pytest.approx(0.0)


# ── 正面情绪 → pad.P > 0 ────────────────────────────────────


class TestPositivePad:
    """正面情绪文本 → pad.P > 0"""

    def test_happy_text_positive_P(self):
        result = detect_emotion("好开心")
        assert result["pad"]["P"] > 0

    def test_excited_text_positive_P(self):
        result = detect_emotion("好兴奋")
        assert result["pad"]["P"] > 0

    def test_positive_intensity_nonzero(self):
        result = detect_emotion("好开心")
        assert result["intensity"] > 0

    def test_positive_valence(self):
        result = detect_emotion("好开心")
        assert result["valence"] == "positive"


# ── 负面情绪 → pad.P < 0 ────────────────────────────────────


class TestNegativePad:
    """负面情绪文本 → pad.P < 0"""

    def test_sad_text_negative_P(self):
        result = detect_emotion("好难过")
        assert result["pad"]["P"] < 0

    def test_angry_text_negative_P(self):
        result = detect_emotion("好生气")
        assert result["pad"]["P"] < 0

    def test_fear_text_negative_P(self):
        result = detect_emotion("好害怕")
        assert result["pad"]["P"] < 0

    def test_negative_valence(self):
        result = detect_emotion("好难过")
        assert result["valence"] == "negative"


# ── 向下兼容: 旧字段仍存在且正确 ───────────────────────────


class TestBackwardCompatibility:
    """旧字段 primary/valence/intensity 仍然存在且正确"""

    def test_old_fields_present_on_neutral(self):
        result = detect_emotion("")
        assert "primary" in result
        assert "valence" in result
        assert "intensity" in result

    def test_old_fields_present_on_emotion(self):
        result = detect_emotion("好开心")
        assert "primary" in result
        assert "valence" in result
        assert "intensity" in result

    def test_neutral_primary_label(self):
        result = detect_emotion("")
        assert result["primary"] == "平静"
        assert result["valence"] == "neutral"
        assert result["intensity"] == 0.0

    def test_happy_primary_label(self):
        result = detect_emotion("好开心")
        assert result["primary"] == "喜悦"

    def test_sad_primary_label(self):
        result = detect_emotion("好难过")
        assert result["primary"] == "悲伤"

    def test_intensity_in_valid_range(self):
        result = detect_emotion("好开心")
        assert 0.0 < result["intensity"] <= 1.0

    def test_pad_consistent_with_intensity(self):
        """有情绪时 pad 偏离 neutral, 强度越大偏离越远"""
        low = detect_emotion("开心")
        high = detect_emotion("开心 开心 开心 开心 开心")
        # 两者 P 都为正
        assert low["pad"]["P"] > 0
        assert high["pad"]["P"] > 0
