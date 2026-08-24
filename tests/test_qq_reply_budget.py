from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import qq_bot_adapter as qq


@pytest.mark.asyncio
async def test_failed_text_send_refunds_reply_budget() -> None:
    class Message:
        async def reply(self, **_kwargs):
            raise RuntimeError("network failed")

    bot = qq.AIQQBot.__new__(qq.AIQQBot)
    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        with pytest.raises(RuntimeError, match="network failed"):
            await bot._send_stream_segment(
                Message(), "hello", passive=True, is_group=True,
                log_key="test.qq_budget",
            )
        assert budget.used == 0
        assert budget.remaining == 5
    finally:
        qq._qq_reply_budget_var.reset(token)


@pytest.mark.asyncio
async def test_failed_media_send_refunds_reply_budget(monkeypatch) -> None:
    class GroupMessage:
        group_openid = "group"
        id = "message"

    monkeypatch.setattr(qq, "GroupMessage", GroupMessage)
    bot = qq.AIQQBot.__new__(qq.AIQQBot)
    bot.api = SimpleNamespace(
        post_group_message=AsyncMock(return_value=None),
    )
    bot._upload_group_base64 = AsyncMock(return_value="file-info")
    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        with pytest.raises(RuntimeError, match="no success result"):
            await bot._send_group_media(
                GroupMessage(), "caption", Path("sticker.png"), None,
            )
        assert budget.used == 0
        assert budget.remaining == 5
    finally:
        qq._qq_reply_budget_var.reset(token)


@pytest.mark.asyncio
async def test_five_successful_text_sends_block_sixth_without_refund() -> None:
    class Message:
        async def reply(self, **_kwargs):
            return True

    bot = qq.AIQQBot.__new__(qq.AIQQBot)
    message = Message()
    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        for _ in range(5):
            assert await bot._send_stream_segment(
                message, "hello", passive=True, is_group=True,
                log_key="test.qq_budget",
            ) is True
        with pytest.raises(qq.QQReplyBudgetExceeded):
            await bot._send_stream_segment(
                message, "blocked", passive=True, is_group=True,
                log_key="test.qq_budget",
            )
        assert budget.used == 5
        assert budget.remaining == 0
    finally:
        qq._qq_reply_budget_var.reset(token)


def test_group_reply_budget_blocks_sixth_platform_send() -> None:
    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        for _ in range(5):
            assert isinstance(qq._next_msg_seq(), int)
        with pytest.raises(qq.QQReplyBudgetExceeded):
            qq._next_msg_seq()
        assert budget.used == 5
        assert budget.remaining == 0
    finally:
        qq._qq_reply_budget_var.reset(token)


@pytest.mark.asyncio
async def test_failed_group_ack_refunds_reply_budget() -> None:
    class Message:
        async def reply(self, **_kwargs):
            raise RuntimeError("network failed")

    bot = qq.AIQQBot.__new__(qq.AIQQBot)
    request = qq.QQPipelineRequest(
        text="hello",
        user_id="qq_user",
        source="qq_group",
        user_openid="member",
        message=Message(),
        is_group=True,
    )
    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        await bot._send_ack(request)
        assert budget.used == 0
        assert budget.remaining == 5
    finally:
        qq._qq_reply_budget_var.reset(token)


@pytest.mark.asyncio
async def test_group_pipeline_uses_one_shared_budget_for_ack_reply_and_media() -> None:
    bot = qq.AIQQBot.__new__(qq.AIQQBot)
    seen: list[qq.QQReplyBudget | None] = []

    async def process(_request):
        seen.append(qq._qq_reply_budget_var.get())
        for _ in range(5):
            qq._next_msg_seq()
        with pytest.raises(qq.QQReplyBudgetExceeded):
            qq._next_msg_seq()
        return SimpleNamespace(reply="done")

    bot._process_with_core = process
    result = await bot._run_message_pipeline(
        SimpleNamespace(id="m", group_openid="g"),
        is_group=True,
        user_input="hello",
        user_id="qq_user",
        openid="member",
        is_master=False,
        image_data=None,
        group_key="g",
    )

    assert result.reply == "done"
    assert seen[0] is not None
    assert qq._qq_reply_budget_var.get() is None


async def test_none_delivery_skips_current_segment_on_recovery() -> None:
    """None（结果不明确）恢复时必须跳过当前段宁丢勿重，且配额照退。"""
    consumed: list[bool] = []

    class FakeBudget:
        def consume(self):
            consumed.append(True)

        def refund(self):
            consumed.append(False)

    from qq_bot_adapter import QQAmbiguousDelivery, _qq_reply_budget_var
    token = _qq_reply_budget_var.set(FakeBudget())
    try:
        async def factory(_msg_seq):
            return None

        with pytest.raises(QQAmbiguousDelivery):
            await qq._budgeted_await(factory)
        assert consumed == [True, False], "None 应消耗并退还配额"
    finally:
        _qq_reply_budget_var.reset(token)

    segs = ["seg-a", "seg-b", "seg-c"]
    remaining = qq.AIQQBot._remaining_segments_after_error(segs, 0, QQAmbiguousDelivery("x"))
    assert remaining == "seg-bseg-c", "不得把当前段并入重发"
