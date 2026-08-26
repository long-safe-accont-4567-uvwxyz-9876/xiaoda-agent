"""WS msg_id 幂等与心跳自取消防护行为测试。

覆盖两个审查确认缺陷：
1. 重复 (conn_id, msg_id) 帧不得双跑副作用：在途 put-if-absent 拒绝；
   完成后短 TTL 内重试直接拒绝（不二次执行）。
2. 心跳超时路径 unregister 不得取消调用者自身——否则 CancelledError 中断
   chat 任务清理导致 LLM/工具任务泄漏。验证方式：把 runner 换入心跳登记表，
   断言 unregister 完成后 runner 未被取消（若实现仍 cancel 当前任务，
   runner 会在 unregister 内部的 await 点被打上 cancelling 并抛出）。
"""
from __future__ import annotations

import asyncio

import pytest

from web.ws_hub import ConnectionManager


class _FakeWs:
    def __init__(self):
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


@pytest.fixture
def hub():
    return ConnectionManager()


# ── track_message_task put-if-absent ─────────────────────────────


@pytest.mark.asyncio
async def test_track_put_if_absent_rejects_in_flight(hub):
    ws = _FakeWs()
    conn_id = hub.register(ws)
    first = asyncio.Event()

    async def _runner():
        try:
            await first.wait()
        except asyncio.CancelledError:
            raise

    task1 = asyncio.create_task(_runner())
    task2 = asyncio.create_task(_runner())

    assert hub.track_message_task(conn_id, "m1", task1) is True
    # 同 key 第二个任务被拒绝且请求取消，不会登记覆盖
    assert hub.track_message_task(conn_id, "m1", task2) is False
    with pytest.raises(asyncio.CancelledError):
        await asyncio.shield(task2)
    assert hub.get_message_task(conn_id, "m1") is task1

    first.set()
    await asyncio.gather(task1, return_exceptions=True)
    hub._heartbeat_tasks[conn_id].cancel()
    await asyncio.gather(hub._heartbeat_tasks[conn_id], return_exceptions=True)


@pytest.mark.asyncio
async def test_duplicate_in_flight_frame_not_dispatched_twice(hub, monkeypatch):
    """同 (conn_id,msg_id) 在途时重复 chat 帧只执行一次处理。"""
    import web.ws_hub as ws_hub_mod

    ws = _FakeWs()
    conn_id = hub.register(ws)

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_handle_chat(conn_id_, msg, msg_id, ws_):
        calls.append(msg_id)
        started.set()
        await release.wait()
        return {"type": "final", "msg_id": msg_id}

    monkeypatch.setattr(ws_hub_mod, "_handle_chat", fake_handle_chat)
    monkeypatch.setattr(ws_hub_mod, "manager", hub, raising=False)

    frame = {"type": "chat", "msg_id": "dup-1", "text": "hi"}
    task1 = asyncio.create_task(
        ws_hub_mod._dispatch_message(conn_id, dict(frame), "chat", ws))
    await asyncio.wait_for(started.wait(), timeout=5)
    task2 = asyncio.create_task(
        ws_hub_mod._dispatch_message(conn_id, dict(frame), "chat", ws))
    await asyncio.gather(task1, task2)
    release.set()
    # cancel() 落地 + writer 任务把 DUPLICATE_IN_FLIGHT 帧写入 FakeWs.sent
    for _ in range(10):
        if any(s.get("code") == "DUPLICATE_IN_FLIGHT" for s in ws.sent):
            break
        await asyncio.sleep(0.02)

    assert calls.count("dup-1") == 1
    dup_errors = [s for s in ws.sent if s.get("code") == "DUPLICATE_IN_FLIGHT"]
    assert len(dup_errors) == 1
    hb = hub._heartbeat_tasks.pop(conn_id, None)
    if hb:
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_msg_id_retry_replayed_within_ttl(hub, monkeypatch):
    """已完成 msg_id 在 TTL 内重试：不再执行处理，回执 DUPLICATE_COMPLETED。"""
    import web.ws_hub as ws_hub_mod

    ws = _FakeWs()
    conn_id = hub.register(ws)

    async def noop():
        return {"type": "final"}

    done = asyncio.create_task(noop())
    assert hub.track_message_task(conn_id, "done-1", done) is True
    await done
    # done_callback 触发需要一轮事件循环
    await asyncio.sleep(0)
    assert hub.get_completed_result_time(conn_id, "done-1") is not None

    calls: list[str] = []

    async def fake_handle_chat(*a, **k):
        calls.append(a[2] if len(a) > 2 else None)
        return {}

    monkeypatch.setattr(ws_hub_mod, "_handle_chat", fake_handle_chat)
    monkeypatch.setattr(ws_hub_mod, "manager", hub, raising=False)

    await ws_hub_mod._dispatch_message(
        conn_id, {"type": "chat", "msg_id": "done-1", "text": "x"}, "chat", ws)
    assert calls == []
    # writer 任务把 DUPLICATE_COMPLETED 帧写入 FakeWs.sent
    for _ in range(10):
        if any(s.get("code") == "DUPLICATE_COMPLETED" for s in ws.sent):
            break
        await asyncio.sleep(0.02)
    dup = [s for s in ws.sent if s.get("code") == "DUPLICATE_COMPLETED"]
    assert len(dup) == 1
    hb = hub._heartbeat_tasks.pop(conn_id, None)
    if hb:
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)


def test_completed_ttl_expires(hub):
    deadline = hub._MSG_RESULT_TTL_SECONDS + 1
    hub._completed_results[("c1", "old")] = (
        __import__("time").monotonic() - deadline)
    assert hub.get_completed_result_time("c1", "old") is None
    assert ("c1", "old") not in hub._completed_results


# ── 心跳超时 unregister 不自取消 ─────────────────────────────────


@pytest.mark.asyncio
async def test_unregister_from_heartbeat_does_not_cancel_caller(hub):
    """heartbeat 任务自身调 unregister：注销完成、chat 任务被清理、自身不被取消。

    把 runner 换入心跳登记表后，unregister 取出的"心跳任务"正是 current_task；
    若实现仍 cancel 当前任务，runner 会在内部 await 处收到 CancelledError，
    release 永不置位导致 wait_for 超时——超时即失败。
    """
    ws = _FakeWs()
    conn_id = hub.register(ws)

    chat_cancelled = asyncio.Event()

    async def long_chat():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            chat_cancelled.set()
            raise

    chat_task = asyncio.create_task(long_chat())
    assert hub.track_message_task(conn_id, "m-chat", chat_task) is True

    released = asyncio.Event()

    async def heartbeat_timeout_path():
        await hub.unregister(conn_id)
        released.set()

    runner = asyncio.create_task(heartbeat_timeout_path())
    hub._heartbeat_tasks[conn_id] = runner
    await asyncio.wait_for(released.wait(), timeout=5)

    # chat 任务必须已被清理
    await asyncio.wait_for(chat_cancelled.wait(), timeout=5)
    assert conn_id not in hub._connections
    # runner 自身未被取消：正常完成而非 CancelledError
    assert not runner.cancelled() and runner.exception() is None


@pytest.mark.asyncio
async def test_unregister_never_cancels_current_task(hub):
    """从登记在案的心跳任务内部调用 unregister 时，当前任务绝不被 cancel。"""
    ws = _FakeWs()
    conn_id = hub.register(ws)

    result: dict = {}

    async def from_heartbeat():
        current = asyncio.current_task()
        try:
            await hub.unregister(conn_id)
            result["self_cancelled"] = current.cancelling() > 0
        except asyncio.CancelledError:
            result["self_cancelled"] = True

    hb_runner = asyncio.create_task(from_heartbeat())
    hub._heartbeat_tasks[conn_id] = hb_runner
    await asyncio.wait_for(hb_runner, timeout=5)
    assert result["self_cancelled"] is False
