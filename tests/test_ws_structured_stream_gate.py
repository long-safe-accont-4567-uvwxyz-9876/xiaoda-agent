from __future__ import annotations

from web.ws_hub import ConnectionManager


def test_legacy_stream_protocol_is_preserved_when_structured_disabled(monkeypatch) -> None:
    import web.ws_hub as hub

    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", False)
    manager = ConnectionManager()
    event = {"type": "stream_text", "msg_id": "m", "delta": "hello"}
    frames = manager._stream_frames("c", event)
    assert len(frames) == 1 and frames[0] is event


def test_structured_protocol_suppresses_legacy_global_tool_event(monkeypatch) -> None:
    import web.ws_hub as hub

    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    event = {"type": "tool_event", "msg_id": "m", "phase": "start"}
    assert manager._stream_frames("c", event) == []
