"""speech_to_text 端点情绪推断功能测试

测试覆盖:
- detect_emotion 集成验证（不依赖 API）
- _infer_emotion 辅助函数：正常文本/空文本/异常安全
"""
from unittest.mock import patch

import pytest

from emotion.emotion_simple import detect_emotion
from web.routers.chat import _infer_emotion


# ── detect_emotion 集成验证 ───────────────────────────────────


class TestDetectEmotionIntegration:
    """验证 emotion_simple.detect_emotion 返回值结构符合 speech_to_text 的需求"""

    def test_emotion_from_happy_text(self):
        result = detect_emotion("我好开心啊")
        assert result["primary"] == "喜悦"
        assert result["intensity"] > 0

    def test_emotion_from_sad_text(self):
        result = detect_emotion("好难过好想哭")
        assert result["primary"] == "悲伤"

    def test_emotion_from_empty_text(self):
        result = detect_emotion("")
        assert result["primary"] == "平静"

    def test_detect_emotion_returns_required_fields(self):
        """验证返回值包含 speech_to_text 需要的 primary 和 intensity 字段"""
        result = detect_emotion("太棒了！")
        assert "primary" in result
        assert "intensity" in result


# ── _infer_emotion 辅助函数 ───────────────────────────────────


class TestInferEmotion:
    """_infer_emotion 将 detect_emotion 结果映射为 speech_to_text 扩展字段"""

    def test_returns_emotion_and_intensity_for_happy_text(self):
        result = _infer_emotion("我好开心啊")
        assert result["emotion"] == "喜悦"
        assert result["intensity"] > 0

    def test_empty_text_returns_calm(self):
        result = _infer_emotion("")
        assert result["emotion"] == "平静"
        assert result["intensity"] == 0.0

    def test_exception_safe_returns_empty_dict(self):
        """detect_emotion 抛异常时返回空字典，不影响主流程"""
        with patch("web.routers.chat.detect_emotion", side_effect=RuntimeError("boom")):
            result = _infer_emotion("任意文本")
        assert result == {}

    def test_result_keys_are_emotion_and_intensity(self):
        result = _infer_emotion("太棒了！")
        assert set(result.keys()) == {"emotion", "intensity"}
