"""跨模块一致性测试：TTS_STYLE_MAP ↔ EMOTION_STYLE_MAP

代码审查发现的测试盲区：
TTS_STYLE_MAP 的值（如 "coquettish"）必须在 EMOTION_STYLE_MAP 中有对应条目，
否则 TTS 引擎会降级到 neutral 风格，导致功能静默退化。
"""
import pytest

from emotion.emotion_enum import TTS_STYLE_MAP, Emotion
from emotion.tts_engine import EMOTION_STYLE_MAP


class TestTTSStyleMapConsistency:
    """验证 TTS_STYLE_MAP 的所有值在 EMOTION_STYLE_MAP 中都有对应条目"""

    def test_all_tts_style_values_exist_in_emotion_style_map(self):
        """TTS_STYLE_MAP 的每个值都必须是 EMOTION_STYLE_MAP 的键"""
        missing = []
        for emotion, style_name in TTS_STYLE_MAP.items():
            if style_name not in EMOTION_STYLE_MAP:
                missing.append(f"{emotion.name} → '{style_name}'")
        assert not missing, (
            f"TTS_STYLE_MAP 中的以下映射在 EMOTION_STYLE_MAP 中缺失对应条目: {missing}. "
            f"这将导致 TTS 引擎降级到 neutral 风格。"
        )

    def test_coquettish_exists_in_emotion_style_map(self):
        """coquettish 风格必须存在（POUT 的 TTS 风格）"""
        assert "coquettish" in EMOTION_STYLE_MAP, (
            "EMOTION_STYLE_MAP 缺少 'coquettish' 条目，"
            "POUT(撒娇) 的 TTS 会降级为 neutral"
        )

    def test_caring_exists_in_emotion_style_map(self):
        """caring 风格必须存在（MOVED 的 TTS 风格）"""
        assert "caring" in EMOTION_STYLE_MAP

    def test_playful_exists_in_emotion_style_map(self):
        """playful 风格必须存在（PLAYFUL 的 TTS 风格）"""
        assert "playful" in EMOTION_STYLE_MAP

    def test_all_style_values_are_non_empty_strings(self):
        """EMOTION_STYLE_MAP 的所有值都必须是非空字符串"""
        for key, value in EMOTION_STYLE_MAP.items():
            assert isinstance(value, str) and len(value) > 0, (
                f"EMOTION_STYLE_MAP['{key}'] 是空值或非字符串"
            )

    @pytest.mark.parametrize("emotion", list(Emotion))
    def test_each_emotion_has_tts_mapping(self, emotion):
        """每种情绪枚举都有 TTS 风格映射"""
        assert emotion in TTS_STYLE_MAP, f"Emotion.{emotion.name} 缺少 TTS 风格映射"
