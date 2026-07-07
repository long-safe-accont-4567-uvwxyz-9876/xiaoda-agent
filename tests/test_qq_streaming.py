"""QQ 私聊流式输出测试。

验证 _send_streaming_reply 及其在 _send_reply_with_sticker 中的集成：
- 短回复不分片
- 长回复分片
- markdown 代码块不被切断
- URL 不被切断
- 环境变量关闭时回退到原 message.reply
- 异常恢复逻辑
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import get_agent_display_name

_XD_NAME = get_agent_display_name("xiaoda")


class FakeMessage:
    """模拟 QQ message 对象，记录所有 reply 调用。"""

    def __init__(self) -> None:
        self.replies: list[dict] = []

    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        self.replies.append({"content": content, "msg_seq": msg_seq})


class FlakyMessage:
    """在指定调用次数时抛出异常的 FakeMessage，用于测试异常恢复。"""

    def __init__(self, fail_on_call: int) -> None:
        self.replies: list[dict] = []
        self.call_count = 0
        self.fail_on_call = fail_on_call

    async def reply(self, content: str = "", msg_seq: int = 0) -> None:
        self.call_count += 1
        if self.call_count == self.fail_on_call:
            raise RuntimeError("模拟发送失败")
        self.replies.append({"content": content, "msg_seq": msg_seq})


class FakeResult:
    """模拟 ProcessResult，所有媒体字段为空。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sticker_path = None
        self.audio_path = None
        self.tts_pending = False
        self.tts_text = ""
        self.video_path = None
        self.image_paths = None
        self.emotion = ""


class FakeAgent:
    """模拟 AgentCore，仅实现 strip_emotion_tag。"""

    def strip_emotion_tag(self, text: str) -> str:
        return text


def _make_bot():
    """构造一个不调用 __init__ 的 AIQQBot 实例用于测试。"""
    from qq_bot_adapter import AIQQBot
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = FakeAgent()
    return bot


# ──────────────────────────────────────────────────────────────
# 测试 1：短回复不分片
# ──────────────────────────────────────────────────────────────
def test_short_reply_no_split():
    """短回复（< 400 字符）应直接发送单片，不应有打字指示。"""
    bot = _make_bot()
    msg = FakeMessage()
    short_text = "小妲收到啦，正在想～🌿"

    asyncio.run(bot._send_streaming_reply(msg, short_text))

    # 应只发送一次（单片），不应有打字指示
    assert len(msg.replies) == 1
    assert msg.replies[0]["content"] == short_text
    assert msg.replies[0]["content"] != f"{_XD_NAME}正在打字..."


# ──────────────────────────────────────────────────────────────
# 测试 2：长回复分片
# ──────────────────────────────────────────────────────────────
def test_long_reply_split():
    """长回复（> 400 字符）应分片发送，首片前应有打字指示。"""
    bot = _make_bot()
    msg = FakeMessage()
    # 570 字符，会切为 2 片
    long_text = "小妲来啦～" + ("今天天气真好呀，我们一起出去玩吧～" * 40)

    # 用 0 延迟避免测试变慢
    async def _no_sleep(_):
        pass

    with patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        asyncio.run(bot._send_streaming_reply(msg, long_text))

    # 应有打字指示 + 至少 2 个分片
    assert len(msg.replies) >= 3
    assert msg.replies[0]["content"] == f"{_XD_NAME}正在打字..."
    # 验证分片内容能拼回原文（去掉打字指示）
    actual = "".join(r["content"] for r in msg.replies[1:])
    assert actual == long_text


# ──────────────────────────────────────────────────────────────
# 测试 3：markdown 代码块不被切断
# ──────────────────────────────────────────────────────────────
def test_markdown_code_block_no_split():
    """切片不应切断 markdown 代码块：每段中 ``` 数量必须为偶数。"""
    bot = _make_bot()
    code_block = "```python\n" + "print('hello world')\n" * 30 + "```"
    text = "来看代码：\n" + code_block + "\n这就是代码～"

    segments = bot._split_text_for_streaming(text, chunk_size=50)

    assert len(segments) > 1
    for seg in segments:
        # 每段要么不含 ```, 要么成对（避免切断代码块）
        assert seg.count('```') % 2 == 0, f"段切在代码块中间: {seg[:60]!r}"

    # 验证拼接后能还原原文
    assert "".join(segments) == text


# ──────────────────────────────────────────────────────────────
# 测试 4：URL 不被切断
# ──────────────────────────────────────────────────────────────
def test_url_no_split():
    """切片不应切断 URL：含 http 的段必须包含完整 URL。"""
    bot = _make_bot()
    url = "https://example.com/very/long/path/with/many/segments/and/more/stuff"
    # 总长 > 400 触发分片
    text = "请看这个链接：" + url + " 很有用哦～" + ("段落内容" * 100)

    segments = bot._split_text_for_streaming(text, chunk_size=30)

    assert len(segments) > 1
    for seg in segments:
        if "http" in seg:
            # 该段包含 URL（完整或部分）；要求完整 URL 在该段中
            assert url in seg, f"URL 被切断：段中含 http 但不包含完整 URL: {seg[:80]!r}"

    # 验证拼接后能还原原文
    assert "".join(segments) == text


# ──────────────────────────────────────────────────────────────
# 测试 5：环境变量关闭时回退到 message.reply
# ──────────────────────────────────────────────────────────────
def test_stream_disabled_when_env_false():
    """QQ_STREAM_REPLY=false 时，_send_streaming_reply 不应被调用。"""
    bot = _make_bot()
    msg = FakeMessage()
    long_text = "x" * 500  # > 400 字符
    result = FakeResult(reply=long_text)

    streaming_calls: list[str] = []

    async def _spy_streaming(message, full_text):
        streaming_calls.append(full_text)

    bot._send_streaming_reply = _spy_streaming

    with patch.dict(os.environ, {"QQ_STREAM_REPLY": "false"}):
        asyncio.run(bot._send_reply_with_sticker(msg, result))

    # 流式方法不应被调用
    assert len(streaming_calls) == 0
    # 应通过原 message.reply 发送（无打字指示）
    assert len(msg.replies) >= 1
    assert not any(r["content"] == f"{_XD_NAME}正在打字..." for r in msg.replies)


# ──────────────────────────────────────────────────────────────
# 测试 6：异常恢复逻辑
# ──────────────────────────────────────────────────────────────
def test_exception_recovery():
    """分片发送失败时，应合并剩余内容为最终片发送，并记录日志。"""
    bot = _make_bot()
    # 第 3 次调用失败（即第 2 个分片失败：1=typing, 2=seg0 ok, 3=seg1 fail）
    msg = FlakyMessage(fail_on_call=3)
    long_text = "小妲来啦～" + ("今天天气真好呀，我们一起出去玩吧～" * 40)

    async def _no_sleep(_):
        pass

    with patch("qq_bot_adapter.asyncio.sleep", _no_sleep):
        asyncio.run(bot._send_streaming_reply(msg, long_text))

    # 调用顺序：1=typing(ok), 2=seg0(ok), 3=seg1(fail), 4=recovery(ok)
    assert msg.call_count == 4
    # 成功记录应有 3 条：typing + seg0 + 合并剩余
    assert len(msg.replies) == 3
    # 第 1 条是打字指示
    assert msg.replies[0]["content"] == f"{_XD_NAME}正在打字..."
    # 最后一条是合并剩余内容（应包含 long_text 的部分内容）
    final_content = msg.replies[-1]["content"]
    assert "今天天气真好呀" in final_content
    # 拼接所有成功内容应能还原原文（去掉打字指示）
    actual = "".join(r["content"] for r in msg.replies[1:])
    assert actual == long_text


# ──────────────────────────────────────────────────────────────
# 测试 7（额外）：环境变量开关默认开启时调用流式
# ──────────────────────────────────────────────────────────────
def test_stream_enabled_by_default():
    """QQ_STREAM_REPLY 未设置时默认启用流式输出。"""
    enabled = os.getenv("QQ_STREAM_REPLY", "true").lower() in ("true", "1", "yes")
    assert enabled is True

    bot = _make_bot()
    msg = FakeMessage()
    long_text = "y" * 500
    result = FakeResult(reply=long_text)

    streaming_calls: list[str] = []

    async def _spy_streaming(message, full_text):
        streaming_calls.append(full_text)
        # 不实际发送，避免 split_long_reply 的随机衔接词干扰断言
        await message.reply(content=full_text, msg_seq=0)

    bot._send_streaming_reply = _spy_streaming

    # 清除环境变量以测试默认值
    env_backup = os.environ.pop("QQ_STREAM_REPLY", None)
    try:
        asyncio.run(bot._send_reply_with_sticker(msg, result))
    finally:
        if env_backup is not None:
            os.environ["QQ_STREAM_REPLY"] = env_backup

    assert len(streaming_calls) == 1
    assert streaming_calls[0] == long_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
