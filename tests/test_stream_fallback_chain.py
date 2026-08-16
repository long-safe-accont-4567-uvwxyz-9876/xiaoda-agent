"""流式调用接入跨 Provider 降级链的验证测试。

验证 _stream_llm_response 在主 Provider 流式失败时：
(a) 无部分内容 → 走 fallback_chat，成功则直接返回，route 不被调用
(b) fallback_chat 返回 None → 回落到 route()
(c) 有部分内容 → 返回带中断提示的部分内容，fallback_chat 不被调用（避免重复内容）
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_raising_stream():
    async def _gen(*args, **kwargs):
        raise RuntimeError("stream boom")
        yield  # pragma: no cover

    return _gen


def _make_partial_then_raise_stream():
    """先 yield 部分内容再抛错，模拟流中途断开。"""
    async def _gen(*args, **kwargs):
        yield "部分内容"
        raise RuntimeError("stream broke mid-way")

    return _gen


def _build_processor(router: MagicMock):
    """构造一个仅挂载 router 的 MessageProcessorMixin 实例。"""
    from agent_core.message_processor import MessageProcessorMixin
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor.router = router
    return processor


@pytest.mark.asyncio
async def test_no_partial_returns_fallback_text():
    """(a) 无部分内容时返回 fallback 文本，route 不被调用。"""
    router = MagicMock()
    router.chat_stream = _make_raising_stream()
    router.fallback_chat = AsyncMock(return_value="fallback text")
    router.route = AsyncMock(return_value="route text")
    processor = _build_processor(router)

    with patch("agent_core.message_processor.STREAM_TEXT_PUSH", True):
        result = await processor._stream_llm_response(
            [{"role": "user", "content": "hi"}],
            task_type="chat",
            temperature=0.7,
            max_tokens=2048,
            user_openid="u1",
            session_id="s1",
        )

    assert result == "fallback text"
    router.fallback_chat.assert_awaited_once()
    router.route.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_returns_none_falls_back_to_route():
    """(b) fallback_chat 返回 None 时回落到 route()。"""
    router = MagicMock()
    router.chat_stream = _make_raising_stream()
    router.fallback_chat = AsyncMock(return_value=None)
    router.route = AsyncMock(return_value="route text")
    processor = _build_processor(router)

    with patch("agent_core.message_processor.STREAM_TEXT_PUSH", True):
        result = await processor._stream_llm_response(
            [{"role": "user", "content": "hi"}],
            task_type="chat",
        )

    assert result == "route text"
    router.fallback_chat.assert_awaited_once()
    router.route.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_content_skips_fallback_chain():
    """(c) 有部分内容时返回中断提示，fallback_chat 不被调用。"""
    router = MagicMock()
    router.chat_stream = _make_partial_then_raise_stream()
    router.fallback_chat = AsyncMock(return_value="fallback text")
    router.route = AsyncMock(return_value="route text")
    processor = _build_processor(router)

    with patch("agent_core.message_processor.STREAM_TEXT_PUSH", True):
        result = await processor._stream_llm_response(
            [{"role": "user", "content": "hi"}],
            task_type="chat",
        )

    assert result.endswith("[⚠️ 内容生成中断，以上为已生成的部分]")
    assert "部分内容" in result
    router.fallback_chat.assert_not_called()
    router.route.assert_not_called()
