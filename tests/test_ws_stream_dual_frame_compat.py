"""WS 流式双帧兼容（legacy 帧 + stream_event v1 信封）回归测试。

背景（P1）：结构化流模式曾把 legacy 帧单方面替换为 stream_event 信封，
已部署前端 bundle 与旧标签页对 stream_event 零处理，重启后旧客户端丢失
全部流式文本与 final 帧（聊天假死）。修复后同一逻辑事件双帧发送：
  - 旧客户端（只认 legacy 同名帧）→ 走 [0] legacy 帧
  - 新前端（消费 stream_event）→ 走 [1] 信封
文本两路均携带绝对 accumulated、不含增量 delta，因此同时消费两路的客户端
也不会重复拼接。
"""
from __future__ import annotations

import asyncio

import web.ws_hub as hub
from web.ws_hub import ConnectionManager


class _DummyWS:
    """占位连接对象：_enqueue 只查 _connections 成员与队列；unregister 需要 close()。"""

    async def close(self) -> None:
        pass


def _attach(manager: ConnectionManager, conn_id: str = "c1") -> None:
    manager._connections[conn_id] = _DummyWS()
    manager._send_queues[conn_id] = asyncio.Queue()


async def _drain(manager: ConnectionManager, conn_id: str = "c1") -> list[dict]:
    q = manager._send_queues[conn_id]
    frames: list[dict] = []
    while not q.empty():
        frames.append(q.get_nowait())
    return frames


# ── 客户端行为模型 ──


def replay_legacy(frames: list[dict]) -> tuple[str, list[dict]]:
    """旧客户端（HEAD 部署 bundle）：stream_text 按 accumulated 绝对覆盖。

    final 与前端 onFinal 同义：content 被 reply 绝对覆盖。
    """
    content = ""
    finals: list[dict] = []
    for f in frames:
        if f.get("type") == "stream_text":
            content = f.get("accumulated") or ""
        elif f.get("type") == "final":
            content = f.get("reply") or ""
            finals.append(f)
    return content, finals


def replay_envelope(frames: list[dict]) -> tuple[str, list[dict]]:
    """新前端 onStreamEvent：version/seq/terminal 门控后的绝对覆盖语义。"""
    content = ""
    states: dict[str, dict] = {}
    finals: list[dict] = []
    for f in frames:
        if f.get("type") != "stream_event" or f.get("version") != 1:
            continue
        msg_id, seq = f.get("msg_id"), f.get("seq")
        if not msg_id or not isinstance(seq, int):
            continue
        st = states.setdefault(msg_id, {"last": 0, "term": False})
        if st["term"] or seq <= st["last"]:
            continue
        st["last"] = seq
        st["term"] = f.get("terminal") is True
        if f.get("event") == "text_delta":
            # 前端：delta 存在则追加；信封帧无 delta → accumulated 绝对覆盖
            delta = f.get("delta") or ""
            content = content + delta if delta else (f.get("accumulated") or "")
        elif f.get("event") == "final":
            finals.append(f)
    return content, finals


def replay_both(frames: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """同时注册了两类 handler 的新前端：按到达顺序处理每一帧。"""
    content = ""
    legacy_finals: list[dict] = []
    env_content, env_finals = "", []
    for f in frames:
        if f.get("type") == "stream_text":
            delta = f.get("delta") or ""
            content = content + delta if delta else (f.get("accumulated") or "")
        elif f.get("type") == "final":
            legacy_finals.append(f)
    env_content, env_finals = replay_envelope(frames)
    final_content = (legacy_finals or env_finals)[0].get("reply", "") if (
        legacy_finals or env_finals) else ""
    return final_content, legacy_finals, env_finals


# ── 用例 ──


async def test_old_client_receives_full_text_and_final_via_legacy_frames(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    mid = "m1"
    for ch in ["你", "好"]:
        assert manager._enqueue("c1", {
            "type": "stream_text", "msg_id": mid, "delta": ch, "accumulated": "",
        })
    assert manager._enqueue("c1", {
        "type": "final", "msg_id": mid, "reply": "你好！", "emotion": "喜悦",
    })
    frames = await _drain(manager)

    content, finals = replay_legacy(frames)
    assert content == "你好！"
    assert len(finals) == 1
    assert finals[0]["reply"] == "你好！"
    assert finals[0]["emotion"] == "喜悦"


async def test_new_client_receives_full_text_and_final_via_stream_event(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    mid = "m2"
    for ch in ["早", "安"]:
        assert manager._enqueue("c1", {
            "type": "stream_text", "msg_id": mid, "delta": ch, "accumulated": "",
        })
    assert manager._enqueue("c1", {
        "type": "final", "msg_id": mid, "reply": "早安",
    })
    frames = await _drain(manager)

    envelopes = [f for f in frames if f.get("type") == "stream_event"]
    assert len(envelopes) == 3
    content, finals = replay_envelope(frames)
    assert content == "早安"
    assert len(finals) == 1 and finals[0]["reply"] == "早安"
    # 信封协议完整性：seq 单调递增，final 携带 terminal 标记
    seqs = [e["seq"] for e in envelopes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert envelopes[-1].get("terminal") is True and envelopes[-1]["event"] == "final"


async def test_dual_processing_client_never_doubles_text(monkeypatch) -> None:
    """新前端同时注册两类 handler：双帧全收也不能出现文本翻倍。"""
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    mid = "m3"
    for ch in ["a", "b", "c"]:
        assert manager._enqueue("c1", {
            "type": "stream_text", "msg_id": mid, "delta": ch, "accumulated": "",
        })
    assert manager._enqueue("c1", {"type": "final", "msg_id": mid, "reply": "abc"})
    frames = await _drain(manager)

    # 两路帧都存在
    assert any(f.get("type") == "stream_text" for f in frames)
    assert any(f.get("type") == "stream_event" for f in frames)

    reply, legacy_finals, env_finals = replay_both(frames)
    assert reply == "abc"
    assert len(legacy_finals) == 1 and len(env_finals) == 1
    # 关键反证：若任一路残留增量 delta，双路拼接会得到 "abcabc"/"aabcbc" 等
    env_texts = [f.get("accumulated", "") for f in frames
                 if f.get("type") == "stream_event" and f.get("event") == "text_delta"]
    assert env_texts == ["a", "ab", "abc"]
    legacy_texts = [f.get("accumulated", "") for f in frames
                    if f.get("type") == "stream_text"]
    assert legacy_texts == ["a", "ab", "abc"]
    for f in frames:
        if f.get("type") in ("stream_text", "stream_event"):
            assert not f.get("delta"), f"frame must be absolute, got delta={f.get('delta')!r}"


async def test_provided_accumulated_is_authoritative(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    assert manager._enqueue("c1", {
        "type": "stream_text", "msg_id": "m4", "delta": "x", "accumulated": "Hello",
    })
    frames = await _drain(manager)
    assert [f.get("accumulated") for f in frames] == ["Hello", "Hello"]


async def test_turn_change_restarts_absolute_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    mid = "m5"
    manager._enqueue("c1", {
        "type": "stream_text", "msg_id": mid, "delta": "A", "turn": 0,
    })
    manager._enqueue("c1", {
        "type": "stream_text", "msg_id": mid, "delta": "B", "turn": 1,
    })
    frames = await _drain(manager)
    texts = [(f.get("type"), f.get("turn"), f.get("accumulated")) for f in frames]
    assert texts == [
        ("stream_text", 0, "A"),
        ("stream_event", 0, "A"),
        ("stream_text", 1, "B"),
        ("stream_event", 1, "B"),
    ]


async def test_late_events_after_terminal_are_suppressed_on_both_channels(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    manager._enqueue("c1", {"type": "final", "msg_id": "m6", "reply": "done"})
    before = manager._send_queues["c1"].qsize()
    assert manager._enqueue("c1", {
        "type": "stream_text", "msg_id": "m6", "delta": "late",
    })
    assert manager._send_queues["c1"].qsize() == before


async def test_error_event_is_dual_frame_with_abort_mapping(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    assert manager._enqueue("c1", {
        "type": "error", "msg_id": "m7", "code": "ABORTED", "message": "stopped",
    })
    frames = await _drain(manager)
    legacy = [f for f in frames if f.get("type") == "error"]
    envelope = [f for f in frames if f.get("type") == "stream_event"]
    assert len(legacy) == 1 and legacy[0]["code"] == "ABORTED"
    assert len(envelope) == 1
    assert envelope[0]["event"] == "abort" and envelope[0].get("terminal") is True


async def test_structured_disabled_sends_single_untouched_frame(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", False)
    manager = ConnectionManager()
    _attach(manager)
    event = {"type": "stream_text", "msg_id": "m8", "delta": "hi"}
    assert manager._enqueue("c1", event)
    frames = await _drain(manager)
    assert frames == [event]


async def test_unmapped_and_msgless_events_pass_through_single_frame(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    greeting = {"type": "greeting", "text": "欢迎"}
    assert manager._enqueue("c1", greeting)
    status = {"type": "status", "stage": "thinking", "text": "..."}  # 无 msg_id
    assert manager._enqueue("c1", status)
    frames = await _drain(manager)
    assert frames == [greeting, status]


async def test_unregister_cleans_stream_sessions(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    manager._enqueue("c1", {
        "type": "stream_text", "msg_id": "m9", "delta": "t",
    })
    assert manager._stream_sessions
    await manager.unregister("c1")
    assert not manager._stream_sessions


async def test_session_lru_bounds_stream_state(monkeypatch) -> None:
    monkeypatch.setattr(hub, "STRUCTURED_STREAM_EVENTS", True)
    manager = ConnectionManager()
    _attach(manager)
    for i in range(manager._MAX_STREAM_SESSIONS + 10):
        assert manager._enqueue("c1", {
            "type": "stream_text", "msg_id": f"m{i}", "delta": "x",
        })
    assert len(manager._stream_sessions) == manager._MAX_STREAM_SESSIONS
