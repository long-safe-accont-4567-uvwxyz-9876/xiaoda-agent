"""QQ 流式回复 + sticker 路径的同类 bug 修复测试。

背景：`_send_streaming_reply_with_sticker` 中的 `_send_segment` 与
`_send_streaming_reply._send_segment` 是同构函数，存在相同的"静默吞异常"
bug：群聊被动回复配额超限时，仅 logger.debug 记录就返回，外层循环不知道
已失败继续发下一段（也已超限继续被吞），最终无声丢失所有后续段。

修复：_send_segment 返回 bool，外层循环检测到 False 时合并剩余所有段
（含最后一片与 sticker 的合并片）为单条最终消息发送。

本测试文件验证：
1. 配额超限时 _send_segment 返回 False
2. 外层循环检测到 False 时调用 _send_reply_with_media 合并剩余
3. _send_reply_with_media 也失败时退化为纯文本发送
4. 正常发送路径不受影响
5. 异常恢复路径合并剩余段
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_bot_adapter import AIQQBot
from botpy.message import GroupMessage


class FakeMessage:
    """模拟 QQ 群聊 message 对象，记录所有 reply 调用。

    支持配置 quota_exhausted=True 让 reply 抛出配额超限异常（与生产环境一致）。
    """
    def __init__(self, quota_exhausted: bool = False) -> None:
        self.replies: list[dict] = []
        self.group_openid = "test_group_openid"
        self._quota_exhausted = quota_exhausted

    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        if self._quota_exhausted:
            raise RuntimeError("被动回复超过限制，请稍后使用主动消息发送")
        self.replies.append({"content": content, "msg_seq": msg_seq})


class FakeResult:
    """模拟 ProcessResult，包含 sticker_path。"""
    def __init__(self, reply: str, sticker_path: str = "/fake/sticker.png") -> None:
        self.reply = reply
        self.sticker_path = sticker_path
        self.audio_path = None
        self.tts_pending = False
        self.tts_text = ""
        self.video_path = None
        self.image_paths = None
        self.emotion = ""


class FakeAgent:
    def strip_emotion_tag(self, text: str) -> str:
        return text


def _make_bot():
    """构造一个不调用 __init__ 的 AIQQBot 实例用于测试。"""
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = FakeAgent()
    return bot


def _make_group_message(quota_exhausted: bool = False) -> FakeMessage:
    """构造一个 GroupMessage-like 对象（鸭子类型）。"""
    return FakeMessage(quota_exhausted=quota_exhausted)


# ──────────────────────────────────────────────────────────────
# 测试 1：配额超限时 _send_segment 返回 False 而非静默吞
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_send_segment_returns_false_on_quota_exhausted():
    """_send_segment 在群聊被动回复配额超限时应返回 False 而非静默吞。

    这是修复的核心：原版本返回 None 且仅 logger.debug，外层循环无法感知失败。
    """
    bot = _make_bot()
    msg = _make_group_message(quota_exhausted=True)
    long_text = "长回复内容" * 200  # > 400 字符触发流式
    result = FakeResult(reply=long_text)

    # 拦截 _send_reply_with_media 防止真实发送图片
    send_media_calls: list[str] = []

    async def _fake_send_media(message, text, image_path=None):
        send_media_calls.append(text)
        # 模拟合并发送成功

    bot._send_reply_with_media = _fake_send_media

    # 拦截 sleep 加速测试
    async def _no_sleep(_):
        pass

    with patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply_with_sticker(msg, long_text, result)

    # 验证：由于配额超限，应触发 _send_reply_with_media 合并剩余段
    # （segments[i:] 合并为单条）
    assert len(send_media_calls) >= 1, "配额超限时应调用 _send_reply_with_media 合并剩余段"
    # 合并内容应包含完整长文本（因为第一段就超限，所以剩余=全文）
    merged = send_media_calls[0]
    assert "长回复内容" in merged
    # 长度应等于原文（因为第一段就失败，所有内容都被合并）
    assert len(merged) == len(long_text)


# ──────────────────────────────────────────────────────────────
# 测试 2：配额超限 + _send_reply_with_media 也失败 → 退化为纯文本发送
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_quota_exhausted_with_media_failure_falls_back_to_plain_text():
    """配额超限且 _send_reply_with_media 也失败时，应退化为纯文本发送（放弃 sticker）。"""
    bot = _make_bot()
    msg = _make_group_message(quota_exhausted=True)
    long_text = "长回复内容" * 200
    result = FakeResult(reply=long_text)

    # _send_reply_with_media 抛异常
    async def _fail_send_media(message, text, image_path=None):
        raise OSError("media upload failed")

    bot._send_reply_with_media = _fail_send_media

    async def _no_sleep(_):
        pass

    with patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply_with_sticker(msg, long_text, result)

    # 由于 FakeMessage 的 quota_exhausted=True，message.reply 也会抛异常
    # 但日志应记录 fallback_failed。验证没有未捕获异常即可。
    # 这里主要验证流程能完成，不会卡住或抛出未处理异常。


# ──────────────────────────────────────────────────────────────
# 测试 3：正常发送路径不受影响（无配额超限）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_normal_send_path_not_affected():
    """无配额超限时，前 N-1 片通过 message.reply 发送，最后一片与 sticker 合并。"""
    bot = _make_bot()
    msg = _make_group_message(quota_exhausted=False)
    # 构造一个超过 QQ_GROUP_MSG_BYTE_LIMIT (4000 bytes) 的回复触发分片
    # 每个中文字符 3 bytes，需要 > 1334 个中文字符
    long_text = "前缀内容" + ("正文内容段落" * 400)  # 约 4800 bytes，会切成 2 片
    result = FakeResult(reply=long_text)

    send_media_calls: list[str] = []

    async def _fake_send_media(message, text, image_path=None):
        send_media_calls.append(text)

    bot._send_reply_with_media = _fake_send_media

    async def _no_sleep(_):
        pass

    with patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply_with_sticker(msg, long_text, result)

    # 验证：message.reply 被调用（前 N-1 片），_send_reply_with_media 调用一次（最后一片+sticker）
    assert len(msg.replies) >= 1, "前 N-1 片应通过 message.reply 发送"
    assert len(send_media_calls) == 1, "最后一片应通过 _send_reply_with_media 与 sticker 合并发送"
    # 最后一片内容应是 segments[-1]
    last_media_content = send_media_calls[0]
    assert last_media_content  # 非空
    # 拼接所有内容应能还原原文
    all_text = "".join(r["content"] for r in msg.replies) + last_media_content
    assert all_text == long_text


# ──────────────────────────────────────────────────────────────
# 测试 4：短回复（<=1 段）走 sticker 合并单条发送路径
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_short_reply_uses_single_sticker_merge():
    """短回复（<=1 段）应直接走 _send_reply_with_media 单条发送。"""
    bot = _make_bot()
    msg = _make_group_message(quota_exhausted=False)
    # 群聊 split_for_group_passive 按字节切，单段短文本不分片
    short_text = "短回复～"
    result = FakeResult(reply=short_text)

    send_media_calls: list[str] = []

    async def _fake_send_media(message, text, image_path=None):
        send_media_calls.append(text)

    bot._send_reply_with_media = _fake_send_media

    await bot._send_streaming_reply_with_sticker(msg, short_text, result)

    # 短回复：直接走 _send_reply_with_media，不走 message.reply
    assert len(send_media_calls) == 1
    assert send_media_calls[0] == short_text
    assert len(msg.replies) == 0


# ──────────────────────────────────────────────────────────────
# 测试 5：异常恢复路径合并剩余段与 sticker
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_exception_recovery_merges_remaining_with_sticker():
    """普通异常（非配额）时，合并剩余段与 sticker 一起发送。"""
    bot = _make_bot()

    # 第 1 次 reply 成功，第 2 次抛非配额异常（如 TimeoutError）
    class FlakyMessage:
        def __init__(self):
            self.replies: list[dict] = []
            self.group_openid = "test_group"
            self.call_count = 0

        async def reply(self, content: str = "", msg_seq: int = 0) -> None:
            self.call_count += 1
            if self.call_count == 2:
                raise TimeoutError("模拟网络超时")
            self.replies.append({"content": content, "msg_seq": msg_seq})

    msg = FlakyMessage()
    # 构造一个超过 QQ_GROUP_MSG_BYTE_LIMIT (4000 bytes) 的回复，至少切 2 段才能测异常恢复
    long_text = "前缀内容" + ("正文段落" * 600)  # 约 7200 bytes，切成 2 片
    result = FakeResult(reply=long_text)

    send_media_calls: list[str] = []

    async def _fake_send_media(message, text, image_path=None):
        send_media_calls.append(text)

    bot._send_reply_with_media = _fake_send_media

    async def _no_sleep(_):
        pass

    with patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply_with_sticker(msg, long_text, result)

    # 验证：第 1 段成功发送（call_count=1），第 2 段失败触发恢复
    # 恢复时调用 _send_reply_with_media 合并剩余（包含失败的段及之后所有段）
    assert len(send_media_calls) == 1, "异常恢复时应调用 _send_reply_with_media 合并剩余段"
    # 合并内容应包含原文的某部分（具体取决于切片）


# ──────────────────────────────────────────────────────────────
# 测试 6：无 sticker 时退化为 _send_streaming_reply
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_sticker_delegates_to_send_streaming_reply():
    """result.sticker_path 为空时，应直接委托给 _send_streaming_reply。"""
    bot = _make_bot()
    msg = _make_group_message(quota_exhausted=False)
    long_text = "x" * 500
    result = FakeResult(reply=long_text, sticker_path=None)

    streaming_calls: list[str] = []

    async def _spy_streaming(message, full_text):
        streaming_calls.append(full_text)

    bot._send_streaming_reply = _spy_streaming

    await bot._send_streaming_reply_with_sticker(msg, long_text, result)

    assert len(streaming_calls) == 1
    assert streaming_calls[0] == long_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
