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
