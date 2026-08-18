"""测试 /compress 斜杠命令（修复 4）。

验证 SlashCommandHandler 能正确处理 /compress 命令，调用 AgentContext.compress_now()
并返回可读的压缩报告。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from slash_commands import (
    COMMAND_DESCRIPTIONS,
    OWNER_ONLY_COMMANDS,
    SlashCommandHandler,
)


def _make_handler_with_mock_context(compress_result: dict | None = None,
                                      raise_exception: Exception | None = None):
    """构造 SlashCommandHandler 实例，注入 mock context。

    Args:
        compress_result: compress_now 返回的 dict（None 表示抛异常）
        raise_exception: compress_now 抛出的异常
    """
    context = MagicMock()
    if raise_exception:
        context.compress_now = AsyncMock(side_effect=raise_exception)
    else:
        context.compress_now = AsyncMock(return_value=compress_result or {
            "before_tokens": 50000,
            "after_tokens": 30000,
            "saved_tokens": 20000,
            "before_messages": 30,
            "after_messages": 15,
            "rounds": 2,
            "max_tokens": 60000,
            "message": "压缩完成：50000 → 30000 tokens（节省 20000）",
        })
    return SlashCommandHandler(context=context), context


def test_compress_registered_in_command_descriptions():
    """/compress 应在 COMMAND_DESCRIPTIONS 中注册。"""
    assert "/compress" in COMMAND_DESCRIPTIONS
    assert "压缩" in COMMAND_DESCRIPTIONS["/compress"]


def test_compress_in_owner_only_commands():
    """/compress 应在 OWNER_ONLY_COMMANDS 中（仅主人可用）。"""
    assert "/compress" in OWNER_ONLY_COMMANDS


def test_cmd_compress_method_exists():
    """SlashCommandHandler 应有 _cmd_compress 方法。"""
    assert hasattr(SlashCommandHandler, "_cmd_compress")


@pytest.mark.asyncio
async def test_compress_returns_readable_report():
    """/compress 应返回包含关键指标的压缩报告。"""
    handler, ctx = _make_handler_with_mock_context()
    result = await handler._cmd_compress("", "test_owner")

    assert isinstance(result, str)
    assert "压缩报告" in result or "压缩" in result
    assert "50000" in result  # before_tokens
    assert "30000" in result  # after_tokens
    assert "20000" in result  # saved_tokens

    # 验证 compress_now 被调用
    ctx.compress_now.assert_awaited_once()


@pytest.mark.asyncio
async def test_compress_handles_no_context():
    """context 未初始化时返回友好提示。"""
    handler = SlashCommandHandler(context=None)
    result = await handler._cmd_compress("", "test_owner")
    assert "上下文还没准备好" in result


@pytest.mark.asyncio
async def test_compress_handles_old_context_without_method():
    """旧版 AgentContext 没有 compress_now 方法时返回提示。"""
    ctx = MagicMock()
    # 删除 compress_now 属性
    if hasattr(ctx, "compress_now"):
        delattr(ctx, "compress_now")
    handler = SlashCommandHandler(context=ctx)
    result = await handler._cmd_compress("", "test_owner")
    assert "不支持手动压缩" in result or "升级" in result


@pytest.mark.asyncio
async def test_compress_handles_exception():
    """compress_now 抛异常时返回友好错误信息。"""
    handler, _ = _make_handler_with_mock_context(
        raise_exception=RuntimeError("mock compress failure")
    )
    result = await handler._cmd_compress("", "test_owner")
    assert "出了点问题" in result
    assert "mock compress failure" in result


@pytest.mark.asyncio
async def test_compress_includes_max_tokens_in_report():
    """压缩报告应包含当前模型的 max_tokens 阈值。"""
    handler, _ = _make_handler_with_mock_context(compress_result={
        "before_tokens": 70000,
        "after_tokens": 40000,
        "saved_tokens": 30000,
        "before_messages": 40,
        "after_messages": 20,
        "rounds": 3,
        "max_tokens": 91750,
        "message": "压缩完成",
    })
    result = await handler._cmd_compress("", "test_owner")
    # 阈值在报告中以千分位逗号格式显示（如 91,750）
    assert "91750" in result.replace(",", "") or "91,750" in result  # max_tokens
    assert "70000" in result.replace(",", "") or "70,000" in result  # before
    assert "40000" in result.replace(",", "") or "40,000" in result  # after


@pytest.mark.asyncio
async def test_compress_handles_zero_saved():
    """未超阈值时 saved_tokens=0，报告仍可正常生成。"""
    handler, _ = _make_handler_with_mock_context(compress_result={
        "before_tokens": 100,
        "after_tokens": 100,
        "saved_tokens": 0,
        "before_messages": 2,
        "after_messages": 2,
        "rounds": 0,
        "max_tokens": 91750,
        "message": "当前上下文 100 tokens，未超阈值 91750，无需压缩",
    })
    result = await handler._cmd_compress("", "test_owner")
    assert "0" in result
    assert "无需压缩" in result


@pytest.mark.asyncio
async def test_compress_command_dispatched_via_handle():
    """通过 handle() 调用 /compress 命令应正确分发。"""
    handler, ctx = _make_handler_with_mock_context()
    # owner 验证 mock
    handler._security = MagicMock()
    handler._security.is_owner.return_value = True

    result = await handler.handle("/compress", "test_owner")
    assert isinstance(result, str)
    assert "压缩" in result or "tokens" in result
    ctx.compress_now.assert_awaited_once()


@pytest.mark.asyncio
async def test_compress_rejected_for_non_owner():
    """非主人调用 /compress 应被拒绝。"""
    handler, _ = _make_handler_with_mock_context()
    handler._security = MagicMock()
    handler._security.is_owner.return_value = False

    result = await handler.handle("/compress", "guest_user")
    assert "只有主人才能用" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
