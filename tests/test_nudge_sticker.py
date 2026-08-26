"""NudgeEngine 表情包：主动问候带贴纸时复用 qq_bot_adapter 统一原语，不带贴纸保持 api 直发。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from emotion.nudge_engine import NudgeEngine


class _MockDB:
    def __init__(self, conn=None):
        self._conn = conn

    async def fetch_one(self, *a, **kw):
        return {"c": 0}

    async def fetch_all(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        return 1


class _MockAnalytics:
    insert_proactive_message = AsyncMock(return_value=None)


class _MockRouter:
    async def route(self, *a, **kw):
        return "你好呀～"


def _make_engine(api=None):
    return NudgeEngine(
        db=_MockDB(),
        analytics=_MockAnalytics(),
        router=_MockRouter(),
        api=api or MagicMock(),
        user_openid="test_openid_123",
        dnd_start=25,  # 永不触发 DND
        dnd_end=-1,
    )


@pytest.mark.asyncio
async def test_send_proactive_with_sticker_uses_qq_adapter(tmp_path, monkeypatch):
    sticker = tmp_path / "happy_1.png"
    sticker.write_bytes(b"\x89PNG")
    engine = _make_engine()

    calls: dict = {}

    async def _fake_send(text, openid="", sticker_path=None):
        calls["text"] = text
        calls["openid"] = openid
        calls["sticker_path"] = sticker_path
        return True

    monkeypatch.setattr("qq_bot_adapter.send_proactive_message", _fake_send)

    ok = await engine._send_proactive("爸爸，好呀～", "care", sticker_path=sticker)

    assert ok is True
    assert calls == {"text": "爸爸，好呀～", "openid": "test_openid_123",
                     "sticker_path": sticker}


@pytest.mark.asyncio
async def test_send_proactive_without_sticker_uses_api_direct():
    api = MagicMock()
    api.post_c2c_message = AsyncMock(return_value=None)
    engine = _make_engine(api)

    ok = await engine._send_proactive("爸爸，好呀～", "care")

    assert ok is True
    api.post_c2c_message.assert_awaited_once_with(
        openid="test_openid_123", content="爸爸，好呀～", msg_type=0)
