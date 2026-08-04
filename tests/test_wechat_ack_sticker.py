"""wechat_bot_adapter ACK 与表情包行为测试"""
import asyncio

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


def test_reply_with_sticker_sends_merged(tmp_path):
    """有 sticker_path 时走图文合并，且 ACK 单独发。"""
    img = tmp_path / "s.png"
    img.write_bytes(b"fake-png-bytes")
    core = FakeCore(FakeResult("回复", sticker_path=str(img)))
    adapter = _make_adapter(core)
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    # ACK 单独文本发送
    assert adapter._ilink_client.sent
    # 图文合并发送
    assert adapter._ilink_client.media_sent
    _, _, text, path = adapter._ilink_client.media_sent[0]
    assert text == "回复"
    assert path == str(img)


def test_reply_without_sticker_sends_text(tmp_path):
    """无 sticker_path 时纯文本回复。"""
    core = FakeCore(FakeResult("回复"))
    adapter = _make_adapter(core)
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    # 真实文本回复（非 ACK）
    assert len(adapter._ilink_client.sent) == 2
    assert adapter._ilink_client.sent[1][2] == "回复"
    assert not adapter._ilink_client.media_sent


def test_reply_sticker_failure_falls_back_to_text(tmp_path):
    """图文合并失败时回退纯文本，不阻塞回复。"""
    img = tmp_path / "s.png"
    img.write_bytes(b"fake-png-bytes")
    core = FakeCore(FakeResult("回复", sticker_path=str(img)))
    adapter = _make_adapter(core)
    # 让底层 client 抛异常
    async def boom(*a, **k):
        raise RuntimeError("upload failed")
    adapter._ilink_client.send_media_message = boom
    asyncio.run(adapter._handle_text_message("你好", "user@im.wechat", "ctx_tok"))
    # 回退纯文本（最后一条是文本回复）
    assert adapter._ilink_client.sent[-1][2] == "回复"


def test_send_sticker_delegates_to_media(tmp_path):
    """send_sticker 只发图（text 为空）。"""
    img = tmp_path / "s.png"
    img.write_bytes(b"fake-png-bytes")
    adapter = _make_adapter(FakeCore(None))
    adapter._last_from_user_id = "user@im.wechat"
    adapter._last_context_token = "ctx_tok"
    ok = asyncio.run(adapter.send_sticker(str(img)))
    assert ok is True
    assert len(adapter._ilink_client.media_sent) == 1
