"""测试按 finish_reason 分类的空回复处理（修复 3）。

验证 get_empty_reply_for_finish_reason() 根据 LLM 返回的 finish_reason
返回不同的兜底文案，替代原来统一的 DEGRADED_REPLY。
"""
from __future__ import annotations

from agent_core._shared import (
    DEGRADED_REPLY,
    EMPTY_REPLY_DEFAULT,
    EMPTY_REPLY_REASON_MESSAGES,
    get_empty_reply_for_finish_reason,
    is_degraded_reply,
)


def test_length_returns_specific_message():
    """finish_reason=length 应返回专用截断文案，不是 DEGRADED_REPLY。"""
    msg = get_empty_reply_for_finish_reason("length")
    assert msg == EMPTY_REPLY_REASON_MESSAGES["length"]
    assert msg != DEGRADED_REPLY
    assert "截断" in msg


def test_content_filter_returns_specific_message():
    """finish_reason=content_filter 应返回敏感过滤文案。"""
    msg = get_empty_reply_for_finish_reason("content_filter")
    assert msg == EMPTY_REPLY_REASON_MESSAGES["content_filter"]
    assert "敏感" in msg or "过滤" in msg


def test_tool_calls_returns_specific_message():
    """finish_reason=tool_calls 应返回工具查询文案。"""
    msg = get_empty_reply_for_finish_reason("tool_calls")
    assert msg == EMPTY_REPLY_REASON_MESSAGES["tool_calls"]
    assert "查资料" in msg or "查" in msg


def test_none_finish_reason_returns_default():
    """finish_reason=None 应返回默认 DEGRADED_REPLY。"""
    msg = get_empty_reply_for_finish_reason(None)
    assert msg == EMPTY_REPLY_DEFAULT == DEGRADED_REPLY


def test_empty_string_finish_reason_returns_default():
    """finish_reason='' 应返回默认 DEGRADED_REPLY。"""
    msg = get_empty_reply_for_finish_reason("")
    assert msg == DEGRADED_REPLY


def test_unknown_finish_reason_returns_default():
    """未知 finish_reason（如 'stop'）应返回默认 DEGRADED_REPLY。"""
    msg = get_empty_reply_for_finish_reason("stop")
    assert msg == DEGRADED_REPLY

    msg2 = get_empty_reply_for_finish_reason("unknown_reason")
    assert msg2 == DEGRADED_REPLY


def test_specific_messages_are_degraded():
    """按 finish_reason 分类的兜底文案也应被识别为 degraded_reply。

    这确保它们不会被写入记忆库污染检索。
    """
    for reason in ("length", "content_filter", "tool_calls"):
        msg = get_empty_reply_for_finish_reason(reason)
        assert is_degraded_reply(msg), f"reason={reason} 的兜底文案应被识别为 degraded: {msg}"


def test_message_uniqueness():
    """不同 finish_reason 应返回不同文案。"""
    msg_length = get_empty_reply_for_finish_reason("length")
    msg_filter = get_empty_reply_for_finish_reason("content_filter")
    msg_tool_calls = get_empty_reply_for_finish_reason("tool_calls")
    msg_default = get_empty_reply_for_finish_reason(None)

    # 4 个应互不相同
    msgs = {msg_length, msg_filter, msg_tool_calls, msg_default}
    assert len(msgs) == 4, f"兜底文案不唯一: {msgs}"


def test_dictionary_contains_all_expected_reasons():
    """EMPTY_REPLY_REASON_MESSAGES 应包含所有预期的 finish_reason。"""
    expected_reasons = {"length", "content_filter", "tool_calls"}
    assert expected_reasons.issubset(EMPTY_REPLY_REASON_MESSAGES.keys())


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
