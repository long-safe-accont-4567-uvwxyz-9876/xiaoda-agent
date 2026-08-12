"""微信 Bot 适配器缺陷回归测试（M1-M4 及部分 Minor）。

每个用例先复现 bug（RED），再验证修复（GREEN）。
"""
import asyncio

import pytest

from ilink_client import SessionExpiredError
from wechat_bot_adapter import WeChatBotAdapter


# ---------------------------------------------------------------------------
# 公共 fake
# ---------------------------------------------------------------------------

def _make_adapter(**over):
    kwargs = dict(db=object(), router=object(), api=None, user_openid="u", core=None)
    kwargs.update(over)
    return WeChatBotAdapter(**kwargs)


class _FakeILinkClient:
    """可注入指定异常/返回的 ILinkClient 替身。"""

    def __init__(self, *, send_media_exc=None, get_updates_seq=None):
        self._send_media_exc = send_media_exc
        self._get_updates_seq = list(get_updates_seq or [])
        self.closed = False

    async def send_media_message(self, to_user_id, context_token, content, image_path):
        if self._send_media_exc is not None:
            raise self._send_media_exc
        return {"ret": 0}

    async def get_updates(self, cursor):
        if not self._get_updates_seq:
            raise SessionExpiredError("expired")
        item = self._get_updates_seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# M1: send_media_message 遇 SessionExpiredError 不得清凭证
# ---------------------------------------------------------------------------

def test_send_media_session_expired_does_not_clear_credentials(monkeypatch):
    """M1: send_media_message 遇会话过期时只标记未连接，不清凭证、不置 _expired。"""
    bot = _make_adapter()
    bot._ilink_client = _FakeILinkClient(send_media_exc=SessionExpiredError("expired"))
    bot._last_from_user_id = "user_a"
    bot._connected = True

    cleared = {"called": False}

    def _fake_clear():
        cleared["called"] = True

    monkeypatch.setattr(bot, "_clear_credentials", _fake_clear)

    ok = asyncio.run(
        bot.send_media_message("", "/tmp/x.png", to_user_id="user_a", context_token="tok")
    )
    assert ok is False
    assert cleared["called"] is False, "send_media 不应清除凭证（应交给 poll W5 恢复）"
    assert bot._expired is False, "send_media 不应置 _expired"
    assert bot._connected is False
