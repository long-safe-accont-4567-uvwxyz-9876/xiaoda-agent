"""QQ 群媒体超额降级纯文本回归（2026-08-29 丢消息修复 3）。

原缺陷：_send_group_media 捕获预算/平台"被动回复超过限制"异常后仅记日志并
正常返回，_send_reply_with_media 视为发送完成——纯文本兜底永不触发，图文双丢。

修复契约：
1. 群媒体被预算耗尽/平台限额异常终止 → _send_reply_with_media 走纯文本兜底
   恰好一次，媒体不重复发送；
2. _send_group_media 不再吞掉限额异常（上抛由调用方统一降级）；
3. 纯文本兜底自身仍受群聊预算约束（预算耗尽即放弃，不无限重试、不抛出）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import qq_bot_adapter as qq


class _GroupMessage:
    group_openid = "group"
    id = "msgid"

    def __init__(self):
        self.replied: list[str] = []

    async def reply(self, *, content, msg_seq=0, **_kwargs):
        self.replied.append(content)
        return True


def _make_bot(post_group_message: AsyncMock) -> qq.AIQQBot:
    bot = qq.AIQQBot.__new__(qq.AIQQBot)
    bot.api = SimpleNamespace(post_group_message=post_group_message)
    bot._upload_group_base64 = AsyncMock(return_value="file-info")
    return bot


@pytest.mark.asyncio
async def test_group_media_passive_limit_triggers_text_fallback(monkeypatch):
    """平台"被动回复超过限制"→ 媒体发送终止，纯文本兜底恰好一次。"""
    monkeypatch.setattr(qq, "GroupMessage", _GroupMessage)
    post = AsyncMock(side_effect=RuntimeError("被动回复超过限制"))
    bot = _make_bot(post)
    message = _GroupMessage()

    await bot._send_reply_with_media(message, "降级文本", image_path=Path("x.png"))

    assert post.await_count == 1, "媒体不重复发送"
    assert message.replied == ["降级文本"], "文本兜底应恰好触发一次"


@pytest.mark.asyncio
async def test_group_media_budget_exhaustion_falls_back_to_text(monkeypatch):
    """群预算耗尽异常同样触发文本兜底；失败的媒体配额已退还，兜底可发出。"""
    monkeypatch.setattr(qq, "GroupMessage", _GroupMessage)
    post = AsyncMock(side_effect=RuntimeError("passive reply limited"))
    bot = _make_bot(post)
    message = _GroupMessage()

    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        await bot._send_reply_with_media(message, "预算降级文本", image_path=Path("x.png"))
    finally:
        qq._qq_reply_budget_var.reset(token)

    assert post.await_count == 1
    assert message.replied == ["预算降级文本"], "配额退还后文本兜底应能发出"
    assert budget.used == 1, "仅文本兜底消耗一次配额（媒体失败已退还）"


@pytest.mark.asyncio
async def test_text_fallback_respects_exhausted_budget(monkeypatch):
    """预算已彻底耗尽：媒体与文本兜底都不发，不抛异常、不无限重试。"""
    monkeypatch.setattr(qq, "GroupMessage", _GroupMessage)
    post = AsyncMock()
    bot = _make_bot(post)
    message = _GroupMessage()

    budget = qq.QQReplyBudget(max_total=5)
    token = qq._qq_reply_budget_var.set(budget)
    try:
        for _ in range(5):  # 先耗尽全部群聊回复配额
            qq._next_msg_seq()
        await bot._send_reply_with_media(message, "发不出的文本", image_path=Path("x.png"))
    finally:
        qq._qq_reply_budget_var.reset(token)

    assert post.await_count == 0, "预算耗尽时媒体不得发送"
    assert message.replied == [], "预算耗尽时兜底发不出即放弃"
    assert budget.used == 5, "不得超发配额"


@pytest.mark.asyncio
async def test_send_group_media_raises_on_passive_limit(monkeypatch):
    """_send_group_media 不再吞掉限额异常——上抛交由调用方统一降级。"""
    monkeypatch.setattr(qq, "GroupMessage", _GroupMessage)
    post = AsyncMock(side_effect=RuntimeError("被动回复超过限制"))
    bot = _make_bot(post)

    with pytest.raises(RuntimeError, match="超过限制"):
        await bot._send_group_media(_GroupMessage(), "文本", Path("x.png"), None)
    assert post.await_count == 1
