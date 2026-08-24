"""P1-6: C2C 流式第 4 段合并后未按字节上限再分割 bug 修复测试。

原 bug:
- qq_bot_adapter.py:_send_streaming_reply 在 C2C 路径按 300 字符切片，
  超过 4 段时把 segments[3:] 合并成一个字符串赋给 segments[3]。
- 合并后的字符串可能远超 QQ C2C 单消息 8000 字节上限（中文 3 字节/字符，
  300 字符切片合并 10 段 = 9000 字节，已超限）。
- 原样发送会被 QQ API 拒绝或静默截断。

修复:
- 合并后调用字节分割（按 7800 字节上限，留 200 字节余量给编码开销），
  逐片发送分割后的结果。
- 失败恢复路径（line 1285 附近）做同样处理。

测试:
1. 短回复不触发合并
2. 4 段以内（无需合并）保持原行为
3. 超过 4 段且合并后超 7800 字节，应再分割逐片发送（每片 ≤ 7800 字节）
4. 失败恢复路径合并的剩余内容也按字节再分割
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from botpy.message import C2CMessage, GroupMessage

from config import get_agent_display_name
from qq_bot_adapter import AIQQBot

_XD_NAME = get_agent_display_name("xiaoda")


class FakeC2CMessage:
    """模拟 QQ C2C message 对象，记录所有 reply 调用。"""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.call_count = 0
        # C2CMessage 必要属性
        self.author = MagicMock()
        self.author.user_openid = "test_user_openid"
        self.id = "test_msg_id"

    async def reply(self, content: str = "", msg_seq: int = 0) -> dict:
        self.call_count += 1
        self.replies.append(content)
        return {"id": "fake_msg"}  # 模拟真实 botpy：成功返回消息 dict


class FlakyC2CMessage:
    """在第 N 次 reply 抛出异常的 C2C message，用于测试失败恢复路径。"""

    def __init__(self, fail_on_call: int) -> None:
        self.replies: list[str] = []
        self.call_count = 0
        self.fail_on_call = fail_on_call
        self.author = MagicMock()
        self.author.user_openid = "test_user_openid"
        self.id = "test_msg_id"

    async def reply(self, content: str = "", msg_seq: int = 0) -> dict:
        self.call_count += 1
        if self.call_count == self.fail_on_call:
            raise RuntimeError("模拟发送失败")
        self.replies.append(content)
        return {"id": "fake_msg"}


class FakeAgent:
    def strip_emotion_tag(self, text: str) -> str:
        return text


def _make_bot():
    """构造一个不调用 __init__ 的 AIQQBot 实例。"""
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = FakeAgent()
    return bot


def _patch_c2c_check():
    """patch isinstance 让 FakeC2CMessage 被识别为 C2CMessage。"""
    original_isinstance = isinstance

    def patched(obj, classinfo):
        if classinfo is C2CMessage and isinstance(obj, (FakeC2CMessage, FlakyC2CMessage)):
            return True
        if classinfo is GroupMessage and isinstance(obj, (FakeC2CMessage, FlakyC2CMessage)):
            return False
        return original_isinstance(obj, classinfo)

    return patch("builtins.isinstance", patched)


async def _no_sleep(_t):
    pass


# ──────────────────────────────────────────────────────────────
# 测试 1：短回复不触发合并
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_short_c2c_reply_no_merge():
    """短回复（< 400 字符）应只发 1 条，无合并。"""
    bot = _make_bot()
    msg = FakeC2CMessage()
    short_text = "短回复～"

    with _patch_c2c_check(), patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply(msg, short_text)

    # 短回复：单片发送，无打字指示
    assert len(msg.replies) == 1
    assert msg.replies[0] == short_text


# ──────────────────────────────────────────────────────────────
# 测试 2：4 段以内保持原行为（无合并）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_within_four_segments_no_merge():
    """4 段以内的回复保持原行为，每段单独发送。"""
    bot = _make_bot()
    msg = FakeC2CMessage()
    # 构造 570 字符（约 1710 字节），切 2 段
    text = "小妲来啦～" + ("今天天气真好呀，我们一起出去玩吧～" * 40)

    with _patch_c2c_check(), patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply(msg, text)

    # 应有打字指示 + 至少 2 个分片
    assert len(msg.replies) >= 3
    assert msg.replies[0] == f"{_XD_NAME}正在打字..."
    # 拼接分片应能还原原文（去掉打字指示）
    actual = "".join(r for r in msg.replies[1:])
    assert actual == text


# ──────────────────────────────────────────────────────────────
# 测试 3：超过 4 段且合并后超 7800 字节，应再分割逐片发送（核心 bug）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_merged_tail_resplit_by_bytes():
    """合并后的第 4 段若超过 7800 字节，应再按字节分割逐片发送。

    P1-6 核心 bug：原版本合并后原样发送，单条可能超 8000 字节被 QQ API 拒绝。
    """
    bot = _make_bot()
    msg = FakeC2CMessage()

    # 构造超长文本：300 字符/段 * 30 段 ≈ 9000 字节 * 3 = 27000 字节
    # 合并 segments[3:] 后 = 27 段 * 300 字符 = 8100 字符 ≈ 24300 字节（中文 3 字节）
    # 远超 7800 字节上限
    text = "起" + ("正文内容段落" * 1500)  # 约 18000 字符 ≈ 54000 字节

    with _patch_c2c_check(), patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply(msg, text)

    # 验证：每条 reply（除打字指示外）都不超过 7800 字节
    for r in msg.replies[1:]:  # 跳过打字指示
        byte_len = len(r.encode('utf-8'))
        assert byte_len <= 7800, (
            f"单条 reply 超过 7800 字节上限：{byte_len} 字节，内容前 60 字符：{r[:60]!r}"
        )

    # 验证：拼接所有 reply（去掉打字指示）应能还原原文
    actual = "".join(r for r in msg.replies[1:])
    assert actual == text, "拼接所有分片应等于原文，无内容丢失"


# ──────────────────────────────────────────────────────────────
# 测试 4：失败恢复路径合并的剩余内容也按字节再分割
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_failure_recovery_path_resplit_by_bytes():
    """失败恢复路径合并剩余内容后，应按字节再分割逐片发送。

    P1-6 覆盖：失败恢复路径（line 1285 附近）也需做同样字节分割处理。
    """
    bot = _make_bot()
    # 第 3 次调用失败（即第 2 个分片失败：1=typing, 2=seg0 ok, 3=seg1 fail）
    msg = FlakyC2CMessage(fail_on_call=3)

    # 构造超长文本，确保失败后合并的 remaining 远超 7800 字节
    text = "起" + ("正文内容段落" * 1500)  # 约 54000 字节

    with _patch_c2c_check(), patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply(msg, text)

    # 验证：每条 reply 都不超过 7800 字节（含 typing 指示，它很短）
    for r in msg.replies:
        byte_len = len(r.encode('utf-8'))
        assert byte_len <= 7800, (
            f"失败恢复路径单条 reply 超过 7800 字节：{byte_len} 字节"
        )

    # 验证：拼接所有成功 reply（去掉打字指示）应能还原原文
    # typing 是第 1 条（replies 中第 0 个），seg0 是第 2 条（replies 中第 1 个）
    # seg1 失败不记录在 replies，recovery 后续片记录在 replies[2:]
    assert msg.replies[0] == f"{_XD_NAME}正在打字...", (
        f"第 1 条应是打字指示，实际 {msg.replies[0]!r}"
    )
    actual = "".join(r for r in msg.replies[1:])  # 跳过 typing
    assert actual == text, (
        "拼接所有成功 reply（去掉 typing）应等于原文，无内容丢失"
    )


# ──────────────────────────────────────────────────────────────
# 测试 5：合并后未超 7800 字节时不再额外分割（避免回归）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_merged_within_byte_limit_no_extra_split():
    """合并后未超 7800 字节时应保持 4 段，不额外分割。"""
    bot = _make_bot()
    msg = FakeC2CMessage()

    # 构造恰好需要 5 段（300 字符/段）的文本：5 * 300 = 1500 字符 ≈ 4500 字节
    # 合并 segments[3:] = 2 段 * 300 = 600 字符 ≈ 1800 字节，未超 7800
    text = "起" + ("正文段落内容" * 250)  # 约 3001 字符 ≈ 9000 字节

    with _patch_c2c_check(), patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        await bot._send_streaming_reply(msg, text)

    # 1 typing + 4 segments（合并后第 4 段未超 7800，不再额外分割）
    assert len(msg.replies) == 5, (
        f"应发 1 typing + 4 segments = 5 条，实际 {len(msg.replies)} 条"
    )
    # 拼接分片应能还原原文
    actual = "".join(r for r in msg.replies[1:])
    assert actual == text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
