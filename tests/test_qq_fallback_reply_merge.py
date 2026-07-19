"""QQ _send_fallback_reply_with_sticker C2C 路径同类 bug 修复测试。

背景：上一轮修复了 _send_streaming_reply._send_segment 和
_send_streaming_reply_with_sticker._send_segment 的静默吞异常 bug。
本次发现 _send_fallback_reply_with_sticker 的 C2C 路径存在同类 bug：

原 bug：
- 循环发送 parts[:-1] 时，某段 message.reply 失败（如配额超限）
- 仅 logger.warning + break + failed=True
- 最终 final_text = parts[-1] + "\n（内容过长部分发送失败）"
- 用户只看到最后一段+错误提示，中间所有段无声丢失

修复（与已修复两处同构）：
- 失败时合并剩余所有段（含当前失败的段 + 之后所有段 + 最后一段）为单条发送
- 合并成功：final_text="" 表示已全部发完，仅发送 sticker
- 合并也失败：退化为原行为（发最后一段+错误提示）

注意：split_long_reply 会给每段附加续接提示词，所以拼接所有 parts 不等于原文。
本测试使用 split_long_reply 的实际输出作为预期。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_bot_adapter import AIQQBot, MAX_REPLY_LEN
from botpy.message import C2CMessage
from utils.text_utils import split_long_reply
import utils.text_utils as _text_utils


def _patch_continuation_hints():
    """固定续接提示词，让 split_long_reply 输出可预测。

    split_long_reply 用 random.choice 从 _SEGMENT_CONTINUATIONS 选续接词，
    导致每次调用结果不同，测试无法稳定比较。
    这里 patch 为固定单元素列表，让所有段都用同一个续接词。
    """
    return patch.object(_text_utils, "_SEGMENT_CONTINUATIONS",
                        ("（测试续接词）",))


class FakeC2CMessage:
    """模拟 C2C message 对象，记录所有 reply 调用。

    支持配置 fail_on_call=N 让第 N 次 reply 抛出异常（模拟配额超限）。
    """
    def __init__(self, fail_on_call: int = -1, error_msg: str = "被动回复超过限制") -> None:
        self.replies: list[str] = []
        self.call_count = 0
        self._fail_on_call = fail_on_call
        self._error_msg = error_msg
        # 模拟 C2CMessage 必要属性
        self.author = MagicMock()
        self.author.user_openid = "test_user_openid"
        self.id = "test_msg_id"

    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        self.call_count += 1
        if self.call_count == self._fail_on_call:
            raise RuntimeError(self._error_msg)
        self.replies.append(content)


class AlwaysFailC2CMessage(FakeC2CMessage):
    """所有 reply 调用都失败的 C2C message。"""
    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        self.call_count += 1
        raise RuntimeError("被动回复超过限制")


class FakeResult:
    def __init__(self, reply: str, sticker_path: str | None = "/fake/sticker.png") -> None:
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
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = FakeAgent()
    return bot


def _patch_c2c_check():
    """patch isinstance 让 FakeC2CMessage 被识别为 C2CMessage。"""
    original_isinstance = isinstance

    def patched(obj, classinfo):
        if classinfo is C2CMessage and isinstance(obj, FakeC2CMessage):
            return True
        from botpy.message import GroupMessage
        if classinfo is GroupMessage and isinstance(obj, FakeC2CMessage):
            return False
        return original_isinstance(obj, classinfo)

    return patch("builtins.isinstance", patched)


def _make_long_text() -> str:
    """构造一个会被 split_long_reply 切成多段的文本（>= 2 段）。"""
    # 每个中文字符 3 bytes，需要 > 8000 bytes 才会被切多段
    # 续接提示也占字节，所以构造 12000+ bytes
    return "前缀内容" + ("正文段落内容" * 1000)


# ──────────────────────────────────────────────────────────────
# 测试 1：第 1 段发送失败 → 合并剩余所有段为单条发送
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_first_part_failure_merges_remaining():
    """第 1 段发送失败时，应合并剩余所有段（含当前失败的段 + 之后所有段 + 最后一段）为单条发送。

    修复点：原版本 break 后只发 parts[-1]+错误提示，中间段无声丢失。
    修复后：合并 parts[i:] 为单条发送，i=0 时即全部 parts。
    """
    bot = _make_bot()
    long_text = _make_long_text()
    result = FakeResult(reply=long_text, sticker_path=None)

    with _patch_continuation_hints():
        parts = split_long_reply(long_text, MAX_REPLY_LEN)
        assert len(parts) >= 2, f"测试前提：long_text 应被切多段，实际 {len(parts)} 段"

        # 第 1 次 reply 失败（模拟配额超限）
        msg = FakeC2CMessage(fail_on_call=1)

        async def _fake_send_media(message, reply, image_path=None, image_url=None):
            pass

        bot._send_reply_with_media = _fake_send_media

        with _patch_c2c_check():
            await bot._send_fallback_reply_with_sticker(msg, long_text, result)

        # 验证：第 1 段失败后，应合并 parts[0:] 为单条发送（包含失败的段）
        # msg.replies 应该只有 1 条（合并的全部内容）
        assert len(msg.replies) == 1, f"合并后应只发 1 条，实际 {len(msg.replies)}"
        # 合并内容应等于 parts[:] 的拼接（i=0 时合并全部）
        expected_merged = "".join(parts)
        assert msg.replies[0] == expected_merged, "合并内容应等于 parts[:] 全部拼接"


# ──────────────────────────────────────────────────────────────
# 测试 2：中间段失败 → 合并剩余段（含最后一段）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_middle_part_failure_merges_remaining():
    """第 2 段失败时，应合并剩余段（含当前失败的段 + 之后所有段 + 最后一段）为单条发送。"""
    bot = _make_bot()
    long_text = _make_long_text()
    result = FakeResult(reply=long_text, sticker_path=None)

    with _patch_continuation_hints():
        parts = split_long_reply(long_text, MAX_REPLY_LEN)
        assert len(parts) >= 2

        # 第 2 次 reply 失败（第 1 段成功）
        msg = FakeC2CMessage(fail_on_call=2)

        async def _fake_send_media(message, reply, image_path=None, image_url=None):
            pass

        bot._send_reply_with_media = _fake_send_media

        with _patch_c2c_check():
            await bot._send_fallback_reply_with_sticker(msg, long_text, result)

        # 验证：第 1 段成功 + 合并 parts[1:] = 2 条 reply
        # 第 1 条 = parts[0]，第 2 条 = parts[1:] 拼接
        assert len(msg.replies) == 2, f"应有 2 条 reply，实际 {len(msg.replies)}"
        assert msg.replies[0] == parts[0], "第 1 条应是 parts[0]"
        assert msg.replies[1] == "".join(parts[1:]), "第 2 条应是 parts[1:] 拼接"


# ──────────────────────────────────────────────────────────────
# 测试 3：合并也失败 → 退化为原行为（发最后一段+错误提示）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_merge_also_fails_falls_back_to_error_hint():
    """合并发送也失败时，应退化为发最后一段+错误提示，流程能完成不抛异常。"""
    bot = _make_bot()
    long_text = _make_long_text()
    result = FakeResult(reply=long_text, sticker_path=None)

    with _patch_continuation_hints():
        parts = split_long_reply(long_text, MAX_REPLY_LEN)

        msg = AlwaysFailC2CMessage()

        async def _fake_send_media(message, reply, image_path=None, image_url=None):
            pass

        bot._send_reply_with_media = _fake_send_media

        with _patch_c2c_check():
            # 不应抛出未处理异常
            await bot._send_fallback_reply_with_sticker(msg, long_text, result)

        # 验证：所有 reply 都失败，但流程完成不卡死
        # 调用次数应 >= 2（第 1 段失败 + 合并尝试失败）
        assert msg.call_count >= 2


# ──────────────────────────────────────────────────────────────
# 测试 4：正常发送路径不受影响（无失败）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_normal_send_path_not_affected():
    """无失败时，所有前段依次发送，最后一段单独发送。"""
    bot = _make_bot()
    long_text = _make_long_text()
    result = FakeResult(reply=long_text, sticker_path=None)

    with _patch_continuation_hints():
        parts = split_long_reply(long_text, MAX_REPLY_LEN)

        msg = FakeC2CMessage(fail_on_call=-1)  # 不失败

        async def _fake_send_media(message, reply, image_path=None, image_url=None):
            pass

        bot._send_reply_with_media = _fake_send_media

        with _patch_c2c_check():
            await bot._send_fallback_reply_with_sticker(msg, long_text, result)

        # 验证：所有 parts 依次发送（前 N-1 段循环 + 最后一段 final_text）
        assert len(msg.replies) == len(parts), \
            f"应发 {len(parts)} 条（每段一条），实际 {len(msg.replies)}"
        for i, expected in enumerate(parts):
            assert msg.replies[i] == expected, f"第 {i} 段内容不匹配"


# ──────────────────────────────────────────────────────────────
# 测试 5：短回复（1 段）走单条发送路径
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_short_reply_single_segment():
    """短回复（<=1 段）应直接走单条发送，不进入分片循环。"""
    bot = _make_bot()
    short_text = "短回复～"
    result = FakeResult(reply=short_text, sticker_path=None)

    msg = FakeC2CMessage(fail_on_call=-1)

    async def _fake_send_media(message, reply, image_path=None, image_url=None):
        pass

    bot._send_reply_with_media = _fake_send_media

    with _patch_continuation_hints(), _patch_c2c_check():
        await bot._send_fallback_reply_with_sticker(msg, short_text, result)

    # 验证：单条发送
    assert len(msg.replies) == 1
    assert msg.replies[0] == short_text


# ──────────────────────────────────────────────────────────────
# 测试 6：合并成功后 sticker 单独发送（final_text="" 跳过空消息）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_merge_success_sends_sticker_separately_no_empty_message():
    """合并发送成功后，final_text=""，sticker 通过 _send_reply_with_media 单独发送，不应发空消息。"""
    bot = _make_bot()
    long_text = _make_long_text()
    result = FakeResult(reply=long_text, sticker_path="/fake/sticker.png")

    with _patch_continuation_hints():
        parts = split_long_reply(long_text, MAX_REPLY_LEN)

        # 第 1 次 reply 失败
        msg = FakeC2CMessage(fail_on_call=1)

        send_media_calls: list[str] = []

        async def _fake_send_media(message, reply, image_path=None, image_url=None):
            send_media_calls.append(reply)

        bot._send_reply_with_media = _fake_send_media

        with _patch_c2c_check():
            await bot._send_fallback_reply_with_sticker(msg, long_text, result)

        # 验证：合并发送成功后
        # 1. msg.replies 应该只有 1 条（合并的全部内容 parts[0:]）
        assert len(msg.replies) == 1, f"应只发 1 条 reply（合并的全部内容），实际 {len(msg.replies)}"
        assert msg.replies[0] == "".join(parts), "合并内容应等于 parts[:] 全部拼接"
        # 2. sticker 应通过 _send_reply_with_media 单独发送一次，content 为空字符串
        assert len(send_media_calls) == 1, f"sticker 应单独发送 1 次，实际 {len(send_media_calls)}"
        assert send_media_calls[0] == "", "合并成功后 final_text 应为空字符串"
        # 3. 不应再发空消息（关键修复点）
        # 检查 msg.replies 中没有空字符串
        for r in msg.replies:
            assert r != "", "不应发送空消息"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
