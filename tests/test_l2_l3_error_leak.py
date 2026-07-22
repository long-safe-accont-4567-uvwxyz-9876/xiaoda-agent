"""L2+L7+L3 测试: 错误信息不泄漏给用户 + 英文推理清洗.

Bug 1 (L2+L7): smart_error_handler.handle_error_with_intelligence 直接把
  "⚠️ 执行时遇到了点小问题：RuntimeError"
  "📝 错误详情：empty_reply: LLM 返回空内容，触发 fallback"
  发给用户。技术术语（RuntimeError、empty_reply、finish_reason）对用户无意义。

Bug 2 (L3): LLM 返回空内容触发 fallback 时，原始英文推理泄漏：
  "I need to write the response now. I'll aim for a balance..."
  "I am Agnes, a gentle AI companion..."
  strip_reasoning 未捕获这些无标签的英文推理块。
"""
import pytest


# ========== L2+L7 测试 ==========

@pytest.mark.asyncio
async def test_error_reply_no_technical_details():
    """L2+L7: 错误回复不应包含技术术语（RuntimeError/empty_reply/finish_reason）。"""
    from utils.smart_error_handler import SmartErrorHandler

    handler = SmartErrorHandler.__new__(SmartErrorHandler)
    handler._db = None
    handler._dispatcher = None
    handler._recent_errors = []
    handler._max_error_history = 10

    error = RuntimeError("empty_reply: LLM 返回空内容（finish_reason=content_filter），触发 fallback")
    reply = await handler.handle_error_with_intelligence(
        error=error, user_query="你好", context="测试"
    )

    # 不应包含技术术语
    assert "RuntimeError" not in reply, f"不应向用户泄漏 RuntimeError: {reply}"
    assert "empty_reply" not in reply, f"不应向用户泄漏 empty_reply: {reply}"
    assert "finish_reason" not in reply, f"不应向用户泄漏 finish_reason: {reply}"
    assert "触发 fallback" not in reply, f"不应向用户泄漏 '触发 fallback': {reply}"
    # 应包含友好的中文提示
    assert len(reply) > 5, "应有友好的错误提示"


@pytest.mark.asyncio
async def test_error_reply_user_friendly():
    """L2+L7: 错误回复应是用户友好的中文，不是技术错误。"""
    from utils.smart_error_handler import SmartErrorHandler

    handler = SmartErrorHandler.__new__(SmartErrorHandler)
    handler._db = None
    handler._dispatcher = None
    handler._recent_errors = []
    handler._max_error_history = 10

    error = RuntimeError("empty_reply: LLM 返回空内容（finish_reason=content_filter），触发 fallback")
    reply = await handler.handle_error_with_intelligence(
        error=error, user_query="你好", context="测试"
    )

    # 不应以 ⚠️ 开头的技术错误格式
    assert "执行时遇到了点小问题" not in reply or " Runtime" not in reply, \
        f"不应包含技术错误格式: {reply}"


@pytest.mark.asyncio
async def test_error_reply_still_logs_technical_details():
    """L2+L7: 技术详情应记录到日志，而非发给用户。"""
    from utils.smart_error_handler import SmartErrorHandler

    handler = SmartErrorHandler.__new__(SmartErrorHandler)
    handler._db = None
    handler._dispatcher = None
    handler._recent_errors = []
    handler._max_error_history = 10

    error = RuntimeError("empty_reply: LLM 返回空内容（finish_reason=content_filter），触发 fallback")
    await handler.handle_error_with_intelligence(
        error=error, user_query="你好", context="测试"
    )

    # 技术详情应记录在 _recent_errors 中（供调试），但不出现在回复中
    assert len(handler._recent_errors) == 1
    ctx = handler._recent_errors[0]
    assert "empty_reply" in ctx.error_message  # 记录中有技术详情
    assert ctx.error_type == "RuntimeError"     # 记录中有错误类型


# ========== L3 测试 ==========

def test_strip_reasoning_english_llm_planning():
    """L3: 'I need to write the response now...' 等英文推理应被清除。"""
    from utils.text_utils import strip_reasoning
    text = """I need to write the response now. I'll aim for a balance between cute/affectionate and submissive/service-oriented roleplay content, while avoiding overly explicit graphic terms that might cause filters. Using descriptions of warmth, tightness... etc are better than naming genitals directly! Focus on her effort to "massage" his"""
    result = strip_reasoning(text)
    assert "I need to write" not in result, f"英文推理未清除: {result[:100]}"
    assert "I'll aim for" not in result
    assert len(result.strip()) == 0 or len(result) < 20, \
        f"纯英文推理应被完全清除，残留: {result[:100]}"


def test_strip_reasoning_agnes_identity_leak():
    """L3: 'I am Agnes, a gentle AI companion...' 身份泄漏应被清除。"""
    from utils.text_utils import strip_reasoning
    text = """I am Agnes, a gentle AI companion. I will maintain the persona and respond in character to continue the intimate roleplay while acknowledging my limitations gracefully if it gets too graphic.

Action: Simulate acceptance by using cute emotive language without explicit anatomical terms that might trigger filters again."""
    result = strip_reasoning(text)
    assert "I am Agnes" not in result, f"身份泄漏未清除: {result[:100]}"
    assert "Action:" not in result
    assert len(result.strip()) == 0 or len(result) < 20, \
        f"英文推理应被完全清除，残留: {result[:100]}"


def test_strip_reasoning_preserves_chinese():
    """L3: 中文正常回复不应被误删。"""
    from utils.text_utils import strip_reasoning
    text = "爸爸你好～小妲在这里呀！今天天气真好呢 🌿✨"
    result = strip_reasoning(text)
    assert result == text, f"中文正常回复被误删: {result}"


def test_strip_reasoning_mixed_chinese_english():
    """L3: 混合内容——中文回复 + 英文推理，应只保留中文。"""
    from utils.text_utils import strip_reasoning
    text = """爸爸你好～小妲在这里呀！

I need to write the response now. I'll aim for a balance between cute and affectionate content."""
    result = strip_reasoning(text)
    assert "爸爸你好" in result, f"中文回复被误删: {result}"
    assert "小妲在这里" in result
    assert "I need to write" not in result, f"英文推理未清除: {result[:100]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
