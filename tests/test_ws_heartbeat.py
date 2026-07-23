"""G5: WebSocket 心跳测试 - 死连接 40s 内清理."""
import asyncio
from unittest.mock import AsyncMock, patch

from web.ws_hub import ConnectionManager


# 真实 asyncio.sleep 引用 —— 在 patch 前捕获，patch 期间用其让出事件循环
_real_sleep = asyncio.sleep


async def _fast_sleep(_seconds):
    """patch 替换：不实际等待，但让出一次事件循环控制权.

    AsyncMock 默认不 yield 到事件循环，导致被 patch 的协程无法推进。
    此处用 real_sleep(0) 让出一次，使心跳协程能跑完一轮 send ping。
    """
    await _real_sleep(0)


async def test_heartbeat_sends_ping_every_30s():
    """心跳协程应每 30s 发送 ping."""
    mgr = ConnectionManager()
    ws = AsyncMock()
    # _connections 直接存 WebSocket 对象（与现有 register/send_to 一致）
    mgr._connections["test"] = ws

    # 加速：patch sleep（不阻塞，仅让出事件循环）
    with patch("web.ws_hub.asyncio.sleep", new=_fast_sleep):
        task = asyncio.create_task(mgr._heartbeat_loop("test"))
        # 让心跳协程跑完一轮（patched sleep 让出后，send ping 应已发出）
        await _real_sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 应至少发过一次 ping
    sent_events = [call.args[0] for call in ws.send_json.call_args_list]
    assert any(e.get("type") == "ping" for e in sent_events)


async def test_heartbeat_cleans_up_dead_connection():
    """无 pong 响应的连接应被清理."""
    mgr = ConnectionManager()
    ws = AsyncMock()
    # send 失败模拟死连接
    ws.send_json = AsyncMock(side_effect=RuntimeError("dead"))
    mgr._connections["dead"] = ws

    with patch("web.ws_hub.asyncio.sleep", new=_fast_sleep):
        await mgr._heartbeat_loop("dead")

    # 死连接应被清理
    assert "dead" not in mgr._connections


async def test_heartbeat_clears_pong_before_sending_ping():
    """Bug 修复验证：evt.clear() 必须在 send_json 之前执行。

    原缺陷：send_json 在 clear 之前，客户端极速 pong 可能在 send 和 clear
    之间到达，被随后的 clear 抹掉，导致心跳错误超时断开连接。
    """
    mgr = ConnectionManager()
    ws = AsyncMock()
    mgr._connections["test"] = ws

    call_order = []

    async def _tracked_send_json(event):
        call_order.append("send_json")

    ws.send_json = _tracked_send_json

    original_clear = asyncio.Event.clear

    def _tracked_clear(self):
        call_order.append("clear")
        original_clear(self)

    with patch("web.ws_hub.asyncio.sleep", new=_fast_sleep):
        with patch.object(asyncio.Event, "clear", _tracked_clear):
            task = asyncio.create_task(mgr._heartbeat_loop("test"))
            await _real_sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    first_clear = next((i for i, c in enumerate(call_order) if c == "clear"), None)
    first_send = next((i for i, c in enumerate(call_order) if c == "send_json"), None)

    assert first_clear is not None, "evt.clear() 应被调用"
    assert first_send is not None, "send_json 应被调用"
    assert first_clear < first_send, (
        f"evt.clear() 必须在 send_json 之前调用，实际顺序: {call_order[:5]}"
    )


async def test_heartbeat_does_not_disconnect_on_fast_pong():
    """验证极速 pong 不会导致连接被错误断开。

    场景：客户端在收到 ping 后极快回复 pong。
    如果 clear 在 send 之后，pong 可能在 clear 之前到达并被错误清除。
    修复后 clear 在 send 之前，pong 总是在 clear 之后到达，不会被漏掉。
    """
    mgr = ConnectionManager()
    ws = AsyncMock()
    mgr._connections["fast"] = ws

    with patch("web.ws_hub.asyncio.sleep", new=_fast_sleep):
        task = asyncio.create_task(mgr._heartbeat_loop("fast"))
        # 让心跳协程跑完一轮（已发送 ping）
        await _real_sleep(0.1)
        # 模拟客户端回复 pong
        evt = mgr._pong_events.get("fast")
        if evt:
            evt.set()
        # 再让出一轮，让心跳协程处理 pong
        await _real_sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 连接不应被 unregister（因为 pong 被正确接收）
    assert "fast" in mgr._connections, "极速 pong 不应导致连接被错误断开"
