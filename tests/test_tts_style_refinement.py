"""TTS 风格映射细化测试

验证 TTS_STYLE_MAP 中 PLAYFUL/MOVED/POUT 三种情绪使用独立细腻风格，
其他映射保持不变，且覆盖全部 16 种 Emotion 枚举值。
"""
import pytest

from emotion.emotion_enum import TTS_STYLE_MAP, Emotion


class TestTtsStyleRefinement:
    """TTS 风格映射细化测试"""

    def test_moved_uses_caring_style(self):
        """感动用 caring（温柔关切），不再是 sad"""
        assert TTS_STYLE_MAP[Emotion.MOVED] == "caring"
        assert TTS_STYLE_MAP[Emotion.MOVED] != "sad"

    def test_playful_uses_independent_style(self):
        """调皮用独立 playful 风格，不再是 happy"""
        assert TTS_STYLE_MAP[Emotion.PLAYFUL] == "playful"
        assert TTS_STYLE_MAP[Emotion.PLAYFUL] != "happy"

    def test_pout_uses_coquettish_style(self):
        """撒娇用 coquettish 风格，不再是 shy"""
        assert TTS_STYLE_MAP[Emotion.POUT] == "coquettish"
        assert TTS_STYLE_MAP[Emotion.POUT] != "shy"

    @pytest.mark.parametrize(
        "emotion,expected_style",
        [
            (Emotion.HAPPY, "happy"),
            (Emotion.EXCITED, "happy"),
            (Emotion.LOVE, "happy"),
            (Emotion.SAD, "sad"),
            (Emotion.ANGRY, "angry"),
            (Emotion.ANXIOUS, "anxious"),
            (Emotion.SHY, "shy"),
            (Emotion.SURPRISED, "fear"),
            (Emotion.CONFUSED, "thinking"),  # BUG-2 修复: 困惑用thinking而非curious
            (Emotion.THINKING, "thinking"),
            (Emotion.NEUTRAL, "neutral"),
            (Emotion.FEAR, "fear"),
            (Emotion.CURIOUS, "curious"),
        ],
    )
    def test_other_mappings_unchanged(self, emotion, expected_style):
        """其他映射保持不变"""
        assert TTS_STYLE_MAP[emotion] == expected_style

    def test_tts_style_map_covers_all_emotions(self):
        """TTS_STYLE_MAP 覆盖所有 16 种 Emotion 枚举值"""
        all_emotions = set(Emotion)
        mapped_emotions = set(TTS_STYLE_MAP.keys())
        # 无遗漏
        missing = all_emotions - mapped_emotions
        assert not missing, f"缺少映射的情绪: {missing}"
        # 无多余
        extra = mapped_emotions - all_emotions
        assert not extra, f"多余的映射键: {extra}"
        # 数量一致
        assert len(TTS_STYLE_MAP) == len(Emotion) == 16
