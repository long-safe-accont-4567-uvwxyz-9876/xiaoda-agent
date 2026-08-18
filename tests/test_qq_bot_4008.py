"""QQ Bot WebSocket 关闭码处理测试。

CodeRabbit finding：4008（限频）被错误纳入 _SESSION_INVALID_CODES，
清空 session_id → 重连走 ws_identify → 丢失未 ACK 的消息（RESUME 本可恢复）。

QQ 官方错误码：
- 4007: 无效的 session id → 必须重新 IDENTIFY
- 4008: 超过限频 → 等待后 RESUME（session 仍有效，不应 reset）
- 4009: session 过期 → 必须重新 IDENTIFY
"""
from types import SimpleNamespace

import pytest


class _FakeBotpyLog:
    def warning(self, *a, **k): pass


@pytest.fixture(autouse=True)
def _stub_botpy_logging(monkeypatch):
    """stub botpy.logging.get_logger 避免 import 副作用。"""
    import sys
    import types
    if "botpy.logging" not in sys.modules:
        m = types.ModuleType("botpy.logging")
        m.get_logger = lambda: _FakeBotpyLog()
        sys.modules["botpy.logging"] = m


@pytest.mark.asyncio
async def test_4008_rate_limit_preserves_session_for_resume(monkeypatch):
    """4008 限频不应清空 session_id，保留 RESUME 能力（不丢消息）。"""
    import qq_bot_adapter

    async def fake_original(self, code, msg):
        pass
    monkeypatch.setattr(qq_bot_adapter, "_original_on_closed", fake_original)

    self_mock = SimpleNamespace(_session={"session_id": "sess-123", "last_seq": 42})
    await qq_bot_adapter._patched_on_closed(self_mock, 4008, "rate limited")

    assert self_mock._session["session_id"] == "sess-123", "4008 限频应保留 session_id 走 RESUME"
    assert self_mock._session["last_seq"] == 42, "4008 限频应保留 last_seq"


@pytest.mark.asyncio
async def test_4007_invalid_session_resets_for_identify(monkeypatch):
    """4007 无效 session 应清空 session_id，重连走 IDENTIFY。"""
    import qq_bot_adapter

    async def fake_original(self, code, msg):
        pass
    monkeypatch.setattr(qq_bot_adapter, "_original_on_closed", fake_original)

    self_mock = SimpleNamespace(_session={"session_id": "sess-123", "last_seq": 42})
    await qq_bot_adapter._patched_on_closed(self_mock, 4007, "invalid session")

    assert self_mock._session["session_id"] == "", "4007 应清空 session_id 走 IDENTIFY"
    assert self_mock._session["last_seq"] == 0


@pytest.mark.asyncio
async def test_4009_session_timeout_resets_for_identify(monkeypatch):
    """4009 session 超时应清空 session_id，重连走 IDENTIFY。"""
    import qq_bot_adapter

    async def fake_original(self, code, msg):
        pass
    monkeypatch.setattr(qq_bot_adapter, "_original_on_closed", fake_original)

    self_mock = SimpleNamespace(_session={"session_id": "sess-123", "last_seq": 42})
    await qq_bot_adapter._patched_on_closed(self_mock, 4009, "session timeout")

    assert self_mock._session["session_id"] == "", "4009 应清空 session_id 走 IDENTIFY"
    assert self_mock._session["last_seq"] == 0
