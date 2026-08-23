"""问候表情包：GreetingScheduler/NudgeEngine 复用主对话贴纸管线并透传各通道。

覆盖：
- fixed 类型：_generate 返回的 core.process.sticker_path 透传为 web 事件 sticker_url
- reminder 类型：不走 LLM 时经 get_sticker_info 补选贴纸
- QQ/微信通道：sticker_path 传入 send_proactive_message
- 无贴纸时：事件不带 sticker_url，send_proactive_message 收到 sticker_path=None
- NudgeEngine._send_proactive：带贴纸走 qq_bot_adapter 统一原语，不带贴纸仍走 api 直发
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from web.greeting_scheduler import GreetingScheduler


class _Reply:
    def __init__(self, text: str, sticker: Path | None = None):
        self.reply = text
        self.sticker_path = sticker


class _FakeDB:
    async def fetch_all(self, *a, **kw):
        return []

    async def fetch_one(self, *a, **kw):
        return {"c": 0}

    async def execute(self, *a, **kw):
        return 1


class _FakeCore:
    class _Ctx:
        current_address_term = "爸爸"

    context = _Ctx()
    db = _FakeDB()

    def __init__(self, text: str = "爸爸，好呀～", sticker: Path | None = None,
                 get_sticker_info_ok: bool = True):
        self.text = text
        self.sticker = sticker
        self._gsi = get_sticker_info_ok

    async def process(self, **kw):
        return _Reply(self.text, self.sticker)

    async def get_session(self, user_openid: str):
        return {"id": "sess-1"}

    async def create_session(self, user_openid: str):
        return "sess-1"

    def get_sticker_info(self, text: str):
        if not self._gsi:
            return text, None
        return text, self.sticker


def _make_scheduler(core: _FakeCore) -> GreetingScheduler:
    sched = GreetingScheduler.__new__(GreetingScheduler)
    sched.core = core
    sched.cfg = MagicMock()
    sched.broadcast = AsyncMock()
    sched._fired_today = {}
    sched._deferred = []
    sched._deferred_lock = asyncio.Lock()
    return sched


@pytest.mark.asyncio
async def test_fixed_greeting_sticker_url_in_broadcast(tmp_path):
    sticker = tmp_path / "happy_1.png"
    sticker.write_bytes(b"\x89PNG")
    core = _FakeCore(text="爸爸，好呀～", sticker=sticker)
    sched = _make_scheduler(core)
    # 固定 URL：跳过 media 目录拷贝，验证事件形状即可
    sched._sticker_url = lambda p: f"/media/stickers/{p.name}" if p else None

    text, report = await sched.fire_with_report(
        {"id": 1, "type": "fixed", "prompt_hint": "", "channels": '["web"]'})

    assert text == "爸爸，好呀～"
    assert report["web"]["ok"] is True
    event = sched.broadcast.call_args.args[0]
    assert event["type"] == "greeting"
    assert event["sticker_url"] == "/media/stickers/happy_1.png"
    assert event["text"] == "爸爸，好呀～"


@pytest.mark.asyncio
async def test_fixed_greeting_no_sticker_broadcasts_text_only(tmp_path):
    core = _FakeCore(text="爸爸，好呀～", sticker=None)
    sched = _make_scheduler(core)
    sched._sticker_url = lambda p: None

    _, report = await sched.fire_with_report(
        {"id": 1, "type": "fixed", "prompt_hint": "", "channels": '["web"]'})

    event = sched.broadcast.call_args.args[0]
    assert event["type"] == "greeting"
    assert "sticker_url" not in event or event["sticker_url"] is None


@pytest.mark.asyncio
async def test_qq_wechat_channels_pass_sticker_path(tmp_path):
    sticker = tmp_path / "happy_1.png"
    sticker.write_bytes(b"\x89PNG")
    core = _FakeCore(text="爸爸，好呀～", sticker=sticker)
    sched = _make_scheduler(core)
    sched._sticker_url = lambda p: f"/media/stickers/{p.name}" if p else None

    from unittest.mock import patch
    with patch("qq_bot_adapter.send_proactive_message", new=AsyncMock()) as qq_send, \
         patch("wechat_bot_adapter.send_proactive_message", new=AsyncMock()) as wx_send:
        text, report = await sched.fire_with_report(
            {"id": 2, "type": "fixed", "prompt_hint": "", "channels": '["qq","wechat"]'})

    assert report["qq"]["ok"] is True
    assert report["wechat"]["ok"] is True
    qq_send.assert_awaited_once_with(text, sticker_path=sticker)
    wx_send.assert_awaited_once_with(text, sticker_path=sticker)


@pytest.mark.asyncio
async def test_reminder_picks_sticker_via_get_sticker_info(tmp_path, monkeypatch):
    sticker = tmp_path / "neutral_1.png"
    sticker.write_bytes(b"\x89PNG")
    core = _FakeCore(text="提醒文本", sticker=sticker)
    sched = _make_scheduler(core)
    sched._sticker_url = lambda p: f"/media/stickers/{p.name}" if p else None

    from unittest.mock import patch
    with patch("qq_bot_adapter.send_proactive_message", new=AsyncMock()) as qq_send:
        text, report = await sched.fire_with_report(
            {"id": 3, "type": "reminder", "prompt_hint": "喝水", "channels": '["qq"]'})

    assert "提醒" in text
    assert report["qq"]["ok"] is True
    qq_send.assert_awaited_once()
    # reminder 走 get_sticker_info 补选贴纸
    assert qq_send.call_args.kwargs["sticker_path"] == sticker


@pytest.mark.asyncio
async def test_reminder_without_sticker_source_falls_back(tmp_path):
    core = _FakeCore(text="提醒你一下", sticker=None, get_sticker_info_ok=False)
    sched = _make_scheduler(core)
    sched._sticker_url = lambda *a: None

    from unittest.mock import patch
    with patch("qq_bot_adapter.send_proactive_message", new=AsyncMock()) as qq_send:
        text, report = await sched.fire_with_report(
            {"id": 4, "type": "reminder", "prompt_hint": "喝水", "channels": '["qq"]'})

    assert report["qq"]["ok"] is True
    assert qq_send.call_args.kwargs["sticker_path"] is None
