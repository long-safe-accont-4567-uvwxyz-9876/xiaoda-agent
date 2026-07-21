"""P1-6 测试: 问候短路在语音模式下应生成 TTS 字段。

Bug: _try_greeting_shortcut 直接返回 ProcessResult(reply=..., emotion="greeting")，
未处理语音模式。当用户开启语音模式（self._voice_mode=True）或检测到语音意图时，
问候不会通过 TTS 播报，与其他 fast path 行为不一致。

修复目标: 当 self._voice_mode=True 时，返回带 tts_pending/tts_text 的 ProcessResult。
"""
import os
from unittest.mock import MagicMock, patch

import pytest


def _make_processor():
    """构造 MessageProcessorMixin 实例（绕过完整初始化）。"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    # 必要属性
    p._voice_mode = False
    p.tts = MagicMock()
    p.tts.available = False
    return p


def test_greeting_shortcut_returns_tts_when_voice_mode_on():
    """语音模式开启时，问候短路应返回带 tts_pending 的结果。"""
    os.environ["ENABLE_GREETING_SHORTCUT"] = "true"
    try:
        p = _make_processor()
        p._voice_mode = True

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None, "问候应命中短路"
        assert result.reply, "应有回复文本"
        # 关键断言：语音模式应触发 TTS
        assert getattr(result, "tts_pending", False) is True, \
            "语音模式开启时问候应返回 tts_pending=True"
        assert getattr(result, "tts_text", "") == result.reply, \
            "tts_text 应等于回复文本"
    finally:
        os.environ.pop("ENABLE_GREETING_SHORTCUT", None)


def test_greeting_shortcut_no_tts_when_voice_mode_off():
    """语音模式关闭时，问候短路不应触发 TTS（保持现有行为）。"""
    os.environ["ENABLE_GREETING_SHORTCUT"] = "true"
    try:
        p = _make_processor()
        p._voice_mode = False

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None
        assert getattr(result, "tts_pending", False) is False, \
            "语音模式关闭时不应触发 TTS"
    finally:
        os.environ.pop("ENABLE_GREETING_SHORTCUT", None)


def test_greeting_shortcut_still_returns_emotion_greeting():
    """修复后 emotion 字段应保持 'greeting'（不破坏现有行为）。"""
    os.environ["ENABLE_GREETING_SHORTCUT"] = "true"
    try:
        p = _make_processor()
        p._voice_mode = False

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None
        assert result.emotion == "greeting"
    finally:
        os.environ.pop("ENABLE_GREETING_SHORTCUT", None)


def test_greeting_shortcut_returns_none_for_non_greeting():
    """非问候输入仍应返回 None（不破坏现有行为）。"""
    os.environ["ENABLE_GREETING_SHORTCUT"] = "true"
    try:
        p = _make_processor()
        p._voice_mode = True  # 即使语音模式开启

        result = p._try_greeting_shortcut("帮我写一个 Python 函数", "user1", "qq_c2c")
        assert result is None
    finally:
        os.environ.pop("ENABLE_GREETING_SHORTCUT", None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
