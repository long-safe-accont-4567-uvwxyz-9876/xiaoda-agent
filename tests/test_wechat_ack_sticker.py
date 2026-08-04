"""wechat_bot_adapter ACK 与表情包行为测试"""
import asyncio
from pathlib import Path

from wechat_bot_adapter import WeChatBotAdapter


class FakeResult:
    def __init__(self, reply, sticker_path=None):
        self.reply = reply
        self.sticker_path = sticker_path


class FakeCore:
    def __init__(self, result):
        self._result = result
        self.called = False

    async def process(self, text, user_id="", source="", user_openid=""):
        self.called = True
        return self._result


class FakeClient:
    def __init__(self):
        self.sent = []
        self.media_sent = []

    async def send_message(self, to_user_id, context_token, text):
        self.sent.append((to_user_id, context_token, text))
        return True

    async def send_media_message(self, to_user_id, context_token, text, image_path):
        self.media_sent.append((to_user_id, context_token, text, image_path))
        return {"ret": 0}


def _make_adapter(core, sticker_path=None):
    adapter = WeChatBotAdapter.__new__(WeChatBotAdapter)
    adapter._core = core
    adapter._ilink_client = FakeClient()
    adapter._last_from_user_id = "user@im.wechat"
    adapter._last_context_token = "ctx_tok"
    adapter._expired = False
    return adapter


def test_ack_sent_before_process():
    """process 前先发送 ACK。"""
    core = FakeCore(FakeResult("回复"))
    adapter = _make_adapter(core)
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    assert core.called
    # ACK 是第一条发送
    assert adapter._ilink_client.sent
    first_text = adapter._ilink_client.sent[0][2]
    assert "收到啦" in first_text or "正在想" in first_text