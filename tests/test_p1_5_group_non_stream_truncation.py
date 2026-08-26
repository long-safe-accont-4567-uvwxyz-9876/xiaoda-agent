"""P1-5: 群聊非流式静默截断尾部 bug 修复测试。

原 bug:
- qq_bot_adapter.py:_send_fallback_reply_with_sticker 的群聊分支调用
  split_for_group_passive(clean_reply) 后只取 segments[0] 发送，
  segments[1..] 全部被丢弃，且无任何截断标记提示用户。

修复:
- 遍历 segments 全部逐条发送（不只 segments[0]）
- 若 len(segments) > 4（QQ 群 ACK 配额），只发前 4 段，第 4 段末尾追加 "\n（…）"

测试:
1. 短回复（1 段）保持原行为
2. 多段（2~4 段）应全部发送，无内容丢失
3. 超过 4 段应只发前 4 段，且第 4 段末尾追加 "\n（…）"
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from botpy.message import C2CMessage, GroupMessage

from qq_bot_adapter import AIQQBot
from utils.text_utils import split_for_group_passive


class FakeGroupMessage:
    """模拟 QQ 群聊 message 对象，记录所有 reply 调用。"""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.group_openid = "test_group_openid"

    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        self.replies.append(content)


class FakeResult:
    """模拟 ProcessResult。sticker_path=None 走纯文本路径。"""

    def __init__(self, reply: str, sticker_path: str | None = None) -> None:
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
    """构造一个不调用 __init__ 的 AIQQBot 实例。"""
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = FakeAgent()
    return bot


def _patch_group_check():
    """patch isinstance 让 FakeGroupMessage 被识别为 GroupMessage。"""
    original_isinstance = isinstance

    def patched(obj, classinfo):
        if classinfo is GroupMessage and isinstance(obj, FakeGroupMessage):
            return True
        if classinfo is C2CMessage and isinstance(obj, FakeGroupMessage):
            return False
        return original_isinstance(obj, classinfo)

    return patch("builtins.isinstance", patched)


# ──────────────────────────────────────────────────────────────
# 测试 1：短回复（1 段）保持原行为，只发 1 条
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_short_group_reply_single_send():
    """短群聊回复（1 段）应只发 1 条 reply，无截断。"""
    bot = _make_bot()
    msg = FakeGroupMessage()
    short_text = "短回复～"
    result = FakeResult(reply=short_text, sticker_path=None)

    async def _fake_send_media(message, reply, image_path=None, image_url=None):
        pass

    bot._send_reply_with_media = _fake_send_media

    with _patch_group_check():
        await bot._send_fallback_reply_with_sticker(msg, short_text, result)

    assert len(msg.replies) == 1, f"短回复应只发 1 条，实际 {len(msg.replies)}"
    assert msg.replies[0] == short_text


# ──────────────────────────────────────────────────────────────
# 测试 2：多段（2~4 段）应全部发送，无内容丢失（核心 bug）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_multi_segment_group_reply_sends_all():
    """群聊回复被切成多段时应全部发送，不丢失尾部。

    这是 P1-5 的核心 bug：原版本只发 segments[0]，segments[1..] 全部丢弃。
    """
    bot = _make_bot()
    msg = FakeGroupMessage()

    # 构造一段超过 4000 字节的文本，会被 split_for_group_passive 切成 2~4 段
    # 每个中文字符 3 字节，4000 字节约 1333 字符
    long_text = "段头内容" + ("正文段落内容" * 600)  # 约 7200 字节，会被切 2 段
    segments = split_for_group_passive(long_text)
    assert len(segments) >= 2, (
        f"测试前提：long_text 应被切 >=2 段，实际 {len(segments)} 段"
    )

    result = FakeResult(reply=long_text, sticker_path=None)

    async def _fake_send_media(message, reply, image_path=None, image_url=None):
        pass

    bot._send_reply_with_media = _fake_send_media

    with _patch_group_check():
        await bot._send_fallback_reply_with_sticker(msg, long_text, result)

    # 验证：所有 segments 都被发送（不只 segments[0]）
    assert len(msg.replies) == len(segments), (
        f"应发 {len(segments)} 条 reply（每段一条），实际 {len(msg.replies)} 条。"
        f"sent={msg.replies!r}"
    )
    for i, expected in enumerate(segments):
        assert msg.replies[i] == expected, (
            f"第 {i} 段内容不匹配：expected={expected[:60]!r} actual={msg.replies[i][:60]!r}"
        )


# ──────────────────────────────────────────────────────────────
# 测试 3：4 段（已达配额上限）应全部发送，无截断标记
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_four_segments_group_reply_sends_all_no_marker():
    """群聊回复恰好 4 段（配额上限）应全部发送，不加截断标记。"""
    bot = _make_bot()
    msg = FakeGroupMessage()

    # 构造约 16000 字节的文本，会被切成 4 段（每段约 4000 字节）
    long_text = "段头" + ("长内容段落" * 1100)  # 约 16500 字节
    segments = split_for_group_passive(long_text)
    assert len(segments) == 4, (
        f"测试前提：long_text 应被切 4 段，实际 {len(segments)} 段"
    )

    result = FakeResult(reply=long_text, sticker_path=None)

    async def _fake_send_media(message, reply, image_path=None, image_url=None):
        pass

    bot._send_reply_with_media = _fake_send_media

    with _patch_group_check():
        await bot._send_fallback_reply_with_sticker(msg, long_text, result)

    # 4 段都应被发送
    assert len(msg.replies) == 4, (
        f"应发 4 条 reply，实际 {len(msg.replies)} 条"
    )
    # 不应追加截断标记（4 段都在配额内）
    for r in msg.replies:
        assert "（…）" not in r, "4 段都在配额内，不应有截断标记"


# ──────────────────────────────────────────────────────────────
# 测试 4：超过 4 段应只发前 4 段，第 4 段末尾追加 "\n（…）"
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_more_than_four_segments_truncated_with_marker():
    """群聊回复超过 4 段时应只发前 4 段，第 4 段末尾追加截断标记。

    split_for_group_passive 默认 max_segments=4，不会返回 >4 段，
    但修复代码应防御性处理 >4 段的情况，避免未来变更导致回归。
    这里通过直接 patch split_for_group_passive 返回 5 段来验证防御逻辑。
    """
    bot = _make_bot()
    msg = FakeGroupMessage()

    fake_segments = ["段1", "段2", "段3", "段4", "段5"]

    result = FakeResult(reply="dummy", sticker_path=None)

    async def _fake_send_media(message, reply, image_path=None, image_url=None):
        pass

    bot._send_reply_with_media = _fake_send_media

    with _patch_group_check(), \
         patch("utils.text_utils.split_for_group_passive", return_value=fake_segments):
        await bot._send_fallback_reply_with_sticker(msg, "dummy", result)

    # 验证：只发前 4 段
    assert len(msg.replies) == 4, (
        f"超过 4 段时应只发前 4 段，实际 {len(msg.replies)} 条"
    )
    # 前 3 段原样发送
    assert msg.replies[0] == "段1"
    assert msg.replies[1] == "段2"
    assert msg.replies[2] == "段3"
    # 第 4 段末尾应追加截断标记
    assert msg.replies[3] == "段4\n（…）", (
        f"第 4 段应追加截断标记，实际 {msg.replies[3]!r}"
    )
    # 第 5 段不应被发送
    assert "段5" not in msg.replies, "第 5 段不应被发送"


# ──────────────────────────────────────────────────────────────
# 测试 5：超长群聊回复所有内容能拼回（不丢字）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_long_group_reply_no_content_loss():
    """群聊超长回复通过 segments 拼接后应能还原原文（无丢字）。"""
    bot = _make_bot()
    msg = FakeGroupMessage()

    long_text = "起头" + ("正文内容段落" * 800)  # 约 14400 字节，切 4 段
    segments = split_for_group_passive(long_text)
    assert len(segments) >= 2

    result = FakeResult(reply=long_text, sticker_path=None)

    async def _fake_send_media(message, reply, image_path=None, image_url=None):
        pass

    bot._send_reply_with_media = _fake_send_media

    with _patch_group_check():
        await bot._send_fallback_reply_with_sticker(msg, long_text, result)

    # 拼接所有 reply 应能还原原文（split_for_group_passive 不加衔接词）
    actual = "".join(msg.replies)
    assert actual == long_text, (
        "拼接所有 reply 后应等于原文，无内容丢失"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
