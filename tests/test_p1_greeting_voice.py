"""P1-6 + CodeRabbit F4 测试: 问候短路在语音模式下应生成 TTS 字段。

Bug: _try_greeting_shortcut 仅检查 self._voice_mode 就设 tts_pending=True，
未检查 TTS 可用性、降级状态、TTS_ASYNC_MODE，与 _build_voice_result 的 5 条件不一致。

修复目标: 当且仅当 voice_mode + tts.available + TTS_ASYNC_MODE + is_feature_available("tts")
全部满足时，才返回带 tts_pending/tts_text 的 ProcessResult。
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
    p.tts.available = False  # 默认不可用，测试中按需开启
    return p


@pytest.fixture(autouse=True)
def _enable_greeting_shortcut(monkeypatch):
    """每个测试前自动设置 ENABLE_GREETING_SHORTCUT=true。"""
    monkeypatch.setenv("ENABLE_GREETING_SHORTCUT", "true")
    yield


def test_greeting_shortcut_returns_tts_when_all_conditions_met():
    """CodeRabbit F4: voice_mode + tts.available + TTS_ASYNC_MODE + 降级正常 → tts_pending=True。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", True), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = True
        p = _make_processor()
        p._voice_mode = True
        p.tts.available = True

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None, "问候应命中短路"
        assert result.reply, "应有回复文本"
        assert getattr(result, "tts_pending", False) is True, \
            "全部条件满足时问候应返回 tts_pending=True"
        assert getattr(result, "tts_text", "") == result.reply, \
            "tts_text 应等于回复文本"


def test_greeting_shortcut_no_tts_when_voice_mode_off():
    """语音模式关闭时，问候短路不应触发 TTS（保持现有行为）。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", True), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = True
        p = _make_processor()
        p._voice_mode = False
        p.tts.available = True

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None
        assert getattr(result, "tts_pending", False) is False, \
            "语音模式关闭时不应触发 TTS"


def test_greeting_shortcut_no_tts_when_tts_unavailable():
    """CodeRabbit F4: TTS 引擎不可用时不应设 tts_pending（避免无效合成尝试）。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", True), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = True
        p = _make_processor()
        p._voice_mode = True
        p.tts.available = False  # TTS 不可用

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None, "问候仍应命中短路"
        assert getattr(result, "tts_pending", False) is False, \
            "TTS 不可用时不应设 tts_pending"
        assert result.reply, "仍应返回文本回复"


def test_greeting_shortcut_no_tts_when_degraded():
    """CodeRabbit F4: 降级模式下不应设 tts_pending（绕过降级策略是 bug）。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", True), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = False  # 降级模式
        p = _make_processor()
        p._voice_mode = True
        p.tts.available = True

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None, "问候仍应命中短路"
        assert getattr(result, "tts_pending", False) is False, \
            "降级模式下不应设 tts_pending（遵守降级策略）"
        assert result.reply, "仍应返回文本回复"


def test_greeting_shortcut_no_tts_when_async_mode_off():
    """CodeRabbit F4: TTS_ASYNC_MODE=False 时不应设 tts_pending（短路是同步的，无法内联合成）。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", False), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = True
        p = _make_processor()
        p._voice_mode = True
        p.tts.available = True

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None, "问候仍应命中短路"
        assert getattr(result, "tts_pending", False) is False, \
            "TTS_ASYNC_MODE=False 时不应设 tts_pending（同步短路无法内联合成）"
        assert result.reply, "仍应返回文本回复"


def test_greeting_shortcut_still_returns_emotion_greeting():
    """修复后 emotion 字段应保持 'greeting'（不破坏现有行为）。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", True), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = True
        p = _make_processor()
        p._voice_mode = False
        p.tts.available = True

        result = p._try_greeting_shortcut("你好", "user1", "qq_c2c")

        assert result is not None
        assert result.emotion == "greeting"


def test_greeting_shortcut_returns_none_for_non_greeting():
    """非问候输入仍应返回 None（不破坏现有行为）。"""
    with patch("agent_core.message_processor.TTS_ASYNC_MODE", True), \
         patch("agent_core.message_processor.get_degradation_strategy") as mock_deg:
        mock_deg.return_value.is_feature_available.return_value = True
        p = _make_processor()
        p._voice_mode = True  # 即使语音模式开启
        p.tts.available = True

        result = p._try_greeting_shortcut("帮我写一个 Python 函数", "user1", "qq_c2c")
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
