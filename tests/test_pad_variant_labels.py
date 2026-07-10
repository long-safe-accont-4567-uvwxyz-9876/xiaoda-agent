"""PAD 微调变体标签覆盖测试

代码审查发现的测试盲区：
recall_and_enact 中的 PAD 微调使用 pad_from_emotion(mem.emotion, 0.5)，
但 EmotionalMemory.emotion 可能存储变体标签（如"开心"而非"喜悦"），
导致 pad_from_emotion 返回 neutral，PAD 微调静默失效。

本测试验证变体标签经过 CN_TO_EN_MAP 归一化后能正确映射到 PAD 值。
"""
import pytest
from memory.emotional_memory import CN_TO_EN_MAP
from emotion.pad_model import from_emotion as pad_from_emotion, EMOTION_PAD_REFERENCE, PADEmotion


class TestPADVariantLabels:
    """变体标签 → PAD 值的正确映射"""

    # 反向映射：英文 → 标准中文（与 emotional_memory.py recall_and_enact 中的 _EN_TO_CN_PAD 一致）
    _EN_TO_CN_PAD = {
        "happy": "喜悦", "excited": "兴奋", "sad": "悲伤",
        "angry": "愤怒", "anxious": "焦虑", "shy": "害羞",
        "confused": "好奇", "thinking": "思考", "fear": "恐惧",
        "neutral": "平静", "playful": "喜悦", "pout": "害羞",
        "surprised": "好奇",
    }

    def _normalize_to_pad(self, emotion_label: str, intensity: float = 0.5) -> PADEmotion:
        """模拟 recall_and_enact 中的标签归一化 + PAD 查表逻辑"""
        en_label = CN_TO_EN_MAP.get(emotion_label, emotion_label.lower())
        cn_standard = self._EN_TO_CN_PAD.get(en_label, emotion_label)
        return pad_from_emotion(cn_standard, intensity)

    @pytest.mark.parametrize("variant,expected_cn", [
        ("开心", "喜悦"),
        ("快乐", "喜悦"),
        ("高兴", "喜悦"),
        ("难过", "悲伤"),
        ("伤心", "悲伤"),
        ("孤独", "悲伤"),
        ("失落", "悲伤"),
        ("生气", "愤怒"),
        ("不满", "愤怒"),
        ("烦躁", "愤怒"),
        ("担心", "焦虑"),
        ("紧张", "焦虑"),
        ("不安", "焦虑"),
        ("害怕", "恐惧"),
        ("恐慌", "恐惧"),
        ("感动", "喜悦"),
        ("调皮", "喜悦"),
        ("撒娇", "害羞"),
        ("惊讶", "好奇"),
        ("困惑", "好奇"),
    ])
    def test_variant_label_normalizes_to_correct_cn(self, variant, expected_cn):
        """变体标签通过 CN_TO_EN_MAP + _EN_TO_CN_PAD 归一化到标准中文"""
        en_label = CN_TO_EN_MAP.get(variant, variant.lower())
        cn_standard = self._EN_TO_CN_PAD.get(en_label, variant)
        assert cn_standard == expected_cn, (
            f"变体 '{variant}' 归一化到 '{cn_standard}'，期望 '{expected_cn}'"
        )

    @pytest.mark.parametrize("variant", [
        "开心", "快乐", "高兴", "难过", "伤心", "孤独", "失落",
        "生气", "不满", "烦躁", "担心", "紧张", "不安",
        "害怕", "恐慌", "感动", "调皮", "撒娇", "惊讶", "困惑",
    ])
    def test_variant_label_produces_non_neutral_pad(self, variant):
        """变体标签归一化后产生非 neutral 的 PAD 值"""
        pad = self._normalize_to_pad(variant, 0.5)
        # 不应是 neutral（P=0, A=0, D=0.5）
        assert not (pad.P == 0.0 and pad.A == 0.0 and pad.D == 0.5), (
            f"变体 '{variant}' 产生了 neutral PAD 值，归一化失败"
        )

    def test_happy_variant_produces_positive_p(self):
        """开心类变体产生 P > 0（正面情绪）"""
        for variant in ["开心", "快乐", "高兴", "感动"]:
            pad = self._normalize_to_pad(variant, 0.5)
            assert pad.P > 0, f"变体 '{variant}' 的 P={pad.P} 应为正值"

    def test_sad_variant_produces_negative_p(self):
        """悲伤类变体产生 P < 0（负面情绪）"""
        for variant in ["难过", "伤心", "孤独", "失落"]:
            pad = self._normalize_to_pad(variant, 0.5)
            assert pad.P < 0, f"变体 '{variant}' 的 P={pad.P} 应为负值"

    def test_angry_variant_produces_negative_p(self):
        """愤怒类变体产生 P < 0"""
        for variant in ["生气", "不满", "烦躁"]:
            pad = self._normalize_to_pad(variant, 0.5)
            assert pad.P < 0, f"变体 '{variant}' 的 P={pad.P} 应为负值"

    def test_fear_variant_produces_negative_p(self):
        """恐惧类变体产生 P < 0"""
        for variant in ["害怕", "恐慌"]:
            pad = self._normalize_to_pad(variant, 0.5)
            assert pad.P < 0, f"变体 '{variant}' 的 P={pad.P} 应为负值"

    def test_anxious_variant_produces_negative_p(self):
        """焦虑类变体产生 P < 0"""
        for variant in ["担心", "紧张", "不安"]:
            pad = self._normalize_to_pad(variant, 0.5)
            assert pad.P < 0, f"变体 '{variant}' 的 P={pad.P} 应为负值"

    def test_unknown_label_falls_back_to_neutral(self):
        """完全未知的标签回退到 neutral（P=0, A=0）"""
        pad = self._normalize_to_pad("完全未知的情绪", 0.5)
        # 未知标签 → PADEmotion.neutral().scale(0.5) = (0, 0, 0.25)
        assert pad.P == 0.0
        assert pad.A == 0.0

    def test_cn_to_en_map_covers_all_pad_reference_keys(self):
        """CN_TO_EN_MAP 覆盖 EMOTION_PAD_REFERENCE 的所有标准标签"""
        for std_label in EMOTION_PAD_REFERENCE:
            assert std_label in CN_TO_EN_MAP, (
                f"标准标签 '{std_label}' 不在 CN_TO_EN_MAP 中"
            )

    def test_all_cn_to_en_values_have_pad_reverse_mapping(self):
        """CN_TO_EN_MAP 的所有英文值都能通过 _EN_TO_CN_PAD 反查到标准中文"""
        unmapped = []
        for cn, en in CN_TO_EN_MAP.items():
            if en not in self._EN_TO_CN_PAD:
                unmapped.append(f"'{cn}' → '{en}' (无 PAD 反查)")
        assert not unmapped, (
            f"以下 CN_TO_EN_MAP 条目的英文值缺少 PAD 反查映射: {unmapped}"
        )
