"""PAD 三维情绪模型单元测试

测试覆盖:
- PADEmotion 数据结构: 创建/clamp/scale/to_dict/from_dict/neutral
- EMOTION_PAD_REFERENCE: 9 类标签都有对应值
- from_emotion: 标签+强度 → PAD, 未知标签返回 neutral, 强度调制正确
- blend: 混合两个 PAD 值, 权重 0/1 边界情况
"""
import pytest

from emotion.pad_model import (
    EMOTION_PAD_REFERENCE,
    PADEmotion,
    blend,
    from_emotion,
)

# ── PADEmotion 数据结构 ──────────────────────────────────────


class TestPADEmotionCreation:
    """PADEmotion 创建与字段访问"""

    def test_create_with_values(self):
        pad = PADEmotion(P=0.5, A=0.3, D=0.7)
        assert pad.P == 0.5
        assert pad.A == 0.3
        assert pad.D == 0.7

    def test_create_with_negative_P(self):
        pad = PADEmotion(P=-0.8, A=0.5, D=0.4)
        assert pad.P == -0.8
        assert pad.A == 0.5
        assert pad.D == 0.4


class TestPADEmotionClamp:
    """clamp 将各维度限制到合法范围"""

    def test_clamp_within_range_unchanged(self):
        pad = PADEmotion(P=0.5, A=0.3, D=0.7)
        clamped = pad.clamp()
        assert clamped.P == 0.5
        assert clamped.A == 0.3
        assert clamped.D == 0.7

    def test_clamp_P_upper_bound(self):
        pad = PADEmotion(P=1.5, A=0.5, D=0.5)
        assert pad.clamp().P == 1.0

    def test_clamp_P_lower_bound(self):
        pad = PADEmotion(P=-1.5, A=0.5, D=0.5)
        assert pad.clamp().P == -1.0

    def test_clamp_A_upper_bound(self):
        pad = PADEmotion(P=0.0, A=1.5, D=0.5)
        assert pad.clamp().A == 1.0

    def test_clamp_A_lower_bound(self):
        pad = PADEmotion(P=0.0, A=-0.5, D=0.5)
        assert pad.clamp().A == 0.0

    def test_clamp_D_upper_bound(self):
        pad = PADEmotion(P=0.0, A=0.5, D=1.5)
        assert pad.clamp().D == 1.0

    def test_clamp_D_lower_bound(self):
        pad = PADEmotion(P=0.0, A=0.5, D=-0.5)
        assert pad.clamp().D == 0.0

    def test_clamp_returns_new_instance(self):
        pad = PADEmotion(P=1.5, A=0.5, D=0.5)
        clamped = pad.clamp()
        assert clamped is not pad
        # 原对象不变
        assert pad.P == 1.5


class TestPADEmotionScale:
    """scale 按强度因子缩放"""

    def test_scale_by_one_unchanged(self):
        pad = PADEmotion(P=0.8, A=0.5, D=0.6)
        scaled = pad.scale(1.0)
        assert scaled.P == pytest.approx(0.8)
        assert scaled.A == pytest.approx(0.5)
        assert scaled.D == pytest.approx(0.6)

    def test_scale_by_half(self):
        pad = PADEmotion(P=0.8, A=0.6, D=0.4)
        scaled = pad.scale(0.5)
        assert scaled.P == pytest.approx(0.4)
        assert scaled.A == pytest.approx(0.3)
        assert scaled.D == pytest.approx(0.2)

    def test_scale_by_two(self):
        pad = PADEmotion(P=0.3, A=0.2, D=0.25)
        scaled = pad.scale(2.0)
        assert scaled.P == pytest.approx(0.6)
        assert scaled.A == pytest.approx(0.4)
        assert scaled.D == pytest.approx(0.5)

    def test_scale_by_zero(self):
        pad = PADEmotion(P=0.8, A=0.5, D=0.6)
        scaled = pad.scale(0.0)
        assert scaled.P == 0.0
        assert scaled.A == 0.0
        assert scaled.D == 0.0

    def test_scale_preserves_negative_P(self):
        pad = PADEmotion(P=-0.7, A=0.3, D=0.2)
        scaled = pad.scale(0.5)
        assert scaled.P == pytest.approx(-0.35)

    def test_scale_returns_new_instance(self):
        pad = PADEmotion(P=0.8, A=0.5, D=0.6)
        scaled = pad.scale(0.5)
        assert scaled is not pad


class TestPADEmotionDict:
    """to_dict / from_dict 往返"""

    def test_to_dict_has_keys(self):
        pad = PADEmotion(P=0.5, A=0.3, D=0.7)
        d = pad.to_dict()
        assert set(d.keys()) == {"P", "A", "D"}

    def test_to_dict_rounds_to_four_decimals(self):
        pad = PADEmotion(P=0.123456, A=0.789012, D=0.555555)
        d = pad.to_dict()
        assert d["P"] == 0.1235
        assert d["A"] == 0.789
        assert d["D"] == 0.5556

    def test_from_dict_creates_pademotion(self):
        d = {"P": 0.5, "A": 0.3, "D": 0.7}
        pad = PADEmotion.from_dict(d)
        assert pad.P == 0.5
        assert pad.A == 0.3
        assert pad.D == 0.7

    def test_from_dict_defaults_when_missing(self):
        pad = PADEmotion.from_dict({})
        assert pad.P == 0.0
        assert pad.A == 0.0
        assert pad.D == 0.5  # D 默认 0.5

    def test_from_dict_partial(self):
        pad = PADEmotion.from_dict({"P": -0.4})
        assert pad.P == -0.4
        assert pad.A == 0.0
        assert pad.D == 0.5

    def test_roundtrip_to_dict_from_dict(self):
        original = PADEmotion(P=0.42, A=0.66, D=0.31)
        d = original.to_dict()
        restored = PADEmotion.from_dict(d)
        assert restored.P == pytest.approx(original.P, abs=1e-4)
        assert restored.A == pytest.approx(original.A, abs=1e-4)
        assert restored.D == pytest.approx(original.D, abs=1e-4)


class TestPADEmotionNeutral:
    """neutral 中性情绪"""

    def test_neutral_values(self):
        n = PADEmotion.neutral()
        assert n.P == 0.0
        assert n.A == 0.0
        assert n.D == 0.5

    def test_neutral_is_classmethod(self):
        # 多次调用返回独立实例
        n1 = PADEmotion.neutral()
        n2 = PADEmotion.neutral()
        assert n1 is not n2
        assert n1.P == n2.P


# ── EMOTION_PAD_REFERENCE 映射表 ─────────────────────────────


class TestEmotionPadReference:
    """9 类中文标签 → PAD 参考值映射"""

    NINE_LABELS = ["喜悦", "兴奋", "悲伤", "愤怒", "焦虑", "害羞", "好奇", "思考", "恐惧"]

    def test_all_nine_labels_present(self):
        for label in self.NINE_LABELS:
            assert label in EMOTION_PAD_REFERENCE, f"缺少标签: {label}"

    def test_values_are_pademotion(self):
        for label, pad in EMOTION_PAD_REFERENCE.items():
            assert isinstance(pad, PADEmotion), f"{label} 的值不是 PADEmotion"

    def test_values_in_valid_range(self):
        for label, pad in EMOTION_PAD_REFERENCE.items():
            assert -1.0 <= pad.P <= 1.0, f"{label} P 越界: {pad.P}"
            assert 0.0 <= pad.A <= 1.0, f"{label} A 越界: {pad.A}"
            assert 0.0 <= pad.D <= 1.0, f"{label} D 越界: {pad.D}"

    def test_positive_emotions_have_positive_P(self):
        assert EMOTION_PAD_REFERENCE["喜悦"].P > 0
        assert EMOTION_PAD_REFERENCE["兴奋"].P > 0

    def test_negative_emotions_have_negative_P(self):
        for label in ["悲伤", "愤怒", "焦虑", "恐惧"]:
            assert EMOTION_PAD_REFERENCE[label].P < 0, f"{label} P 应为负"


# ── from_emotion 函数 ────────────────────────────────────────


class TestFromEmotion:
    """from_emotion: 标签 + 强度 → PAD"""

    def test_known_label_full_intensity(self):
        pad = from_emotion("喜悦", 1.0)
        base = EMOTION_PAD_REFERENCE["喜悦"]
        assert pad.P == pytest.approx(base.P)
        assert pad.A == pytest.approx(base.A)
        assert pad.D == pytest.approx(base.D)

    def test_known_label_half_intensity(self):
        pad = from_emotion("喜悦", 0.5)
        base = EMOTION_PAD_REFERENCE["喜悦"]
        assert pad.P == pytest.approx(base.P * 0.5)
        assert pad.A == pytest.approx(base.A * 0.5)
        assert pad.D == pytest.approx(base.D * 0.5)

    def test_known_label_zero_intensity(self):
        pad = from_emotion("愤怒", 0.0)
        assert pad.P == 0.0
        assert pad.A == 0.0
        assert pad.D == 0.0

    def test_unknown_label_returns_neutral_scaled(self):
        pad = from_emotion("不存在的情绪", 1.0)
        n = PADEmotion.neutral()
        assert pad.P == pytest.approx(n.P)
        assert pad.A == pytest.approx(n.A)
        assert pad.D == pytest.approx(n.D)

    def test_unknown_label_zero_intensity(self):
        pad = from_emotion("未知", 0.0)
        assert pad.P == 0.0
        assert pad.A == 0.0
        assert pad.D == 0.0

    def test_intensity_modulates_amplitude(self):
        """强度越大, 偏离中性越远"""
        full = from_emotion("悲伤", 1.0)
        half = from_emotion("悲伤", 0.5)
        # |P_full| > |P_half|
        assert abs(full.P) > abs(half.P)

    def test_default_intensity_is_one(self):
        pad = from_emotion("好奇")
        base = EMOTION_PAD_REFERENCE["好奇"]
        assert pad.P == pytest.approx(base.P)

    def test_result_is_clamped(self):
        """from_emotion 结果应在合法范围内"""
        pad = from_emotion("兴奋", 2.0)
        assert pad.P <= 1.0
        assert pad.A <= 1.0
        assert pad.D <= 1.0


# ── blend 函数 ───────────────────────────────────────────────


class TestBlend:
    """blend: 混合两个 PAD 值"""

    def test_blend_weight_zero_returns_first(self):
        pad1 = PADEmotion(P=0.8, A=0.5, D=0.6)
        pad2 = PADEmotion(P=-0.7, A=0.3, D=0.2)
        result = blend(pad1, pad2, 0.0)
        assert result.P == pytest.approx(pad1.P)
        assert result.A == pytest.approx(pad1.A)
        assert result.D == pytest.approx(pad1.D)

    def test_blend_weight_one_returns_second(self):
        pad1 = PADEmotion(P=0.8, A=0.5, D=0.6)
        pad2 = PADEmotion(P=-0.7, A=0.3, D=0.2)
        result = blend(pad1, pad2, 1.0)
        assert result.P == pytest.approx(pad2.P)
        assert result.A == pytest.approx(pad2.A)
        assert result.D == pytest.approx(pad2.D)

    def test_blend_weight_half_is_average(self):
        pad1 = PADEmotion(P=0.8, A=0.6, D=0.4)
        pad2 = PADEmotion(P=0.2, A=0.2, D=0.8)
        result = blend(pad1, pad2, 0.5)
        assert result.P == pytest.approx(0.5)
        assert result.A == pytest.approx(0.4)
        assert result.D == pytest.approx(0.6)

    def test_blend_default_weight_is_half(self):
        pad1 = PADEmotion(P=0.8, A=0.6, D=0.4)
        pad2 = PADEmotion(P=0.2, A=0.2, D=0.8)
        result = blend(pad1, pad2)
        assert result.P == pytest.approx(0.5)

    def test_blend_weight_clamped(self):
        """权重超出 [0,1] 被钳制"""
        pad1 = PADEmotion(P=0.8, A=0.5, D=0.6)
        pad2 = PADEmotion(P=-0.7, A=0.3, D=0.2)
        result = blend(pad1, pad2, 5.0)
        # weight 钳到 1.0, 等于 pad2
        assert result.P == pytest.approx(pad2.P)

    def test_blend_negative_weight_clamped(self):
        pad1 = PADEmotion(P=0.8, A=0.5, D=0.6)
        pad2 = PADEmotion(P=-0.7, A=0.3, D=0.2)
        result = blend(pad1, pad2, -0.5)
        # weight 钳到 0.0, 等于 pad1
        assert result.P == pytest.approx(pad1.P)

    def test_blend_result_in_valid_range(self):
        pad1 = PADEmotion(P=1.0, A=1.0, D=1.0)
        pad2 = PADEmotion(P=-1.0, A=0.0, D=0.0)
        result = blend(pad1, pad2, 0.5)
        assert -1.0 <= result.P <= 1.0
        assert 0.0 <= result.A <= 1.0
        assert 0.0 <= result.D <= 1.0
