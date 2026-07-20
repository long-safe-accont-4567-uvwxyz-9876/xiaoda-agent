"""G2: WS broadcast 背压测试 - 慢连接不阻塞快连接."""
import asyncio
from unittest.mock import AsyncMock

from web.ws_hub import ConnectionManager


async def test_broadcast_does_not_block_on_slow_connection():
    """1 个慢连接不应阻塞其他连接.

    原 broadcast 串行 send_to，慢连接会阻塞快连接导致整体超时。
    新实现：fire-and-forget 扇出 + 5s 超时，慢连接超时后 unregister。
    """
    mgr = ConnectionManager()

    # 模拟 3 个连接：1 慢 2 快
    slow_ws = AsyncMock()

    async def slow_send(*a, **kw):
        await asyncio.sleep(10)  # 模拟慢连接
    slow_ws.send_json = slow_send

    fast_ws1 = AsyncMock()
    fast_ws2 = AsyncMock()

    # _connections 直接存 WebSocket 对象（与 send_to/register 一致）
    mgr._connections = {
        "slow": slow_ws,
        "fast1": fast_ws1,
        "fast2": fast_ws2,
    }

    # 广播事件：5s 内部超时 + 外层 10s 保护
    event = {"type": "test"}
    await asyncio.wait_for(mgr.broadcast(event), timeout=10.0)

    # 快连接应收到事件
    fast_ws1.send_json.assert_called_once_with(event)
    fast_ws2.send_json.assert_called_once_with(event)
    # 慢连接应被清理（从 _connections 移除）
    assert "slow" not in mgr._connections


async def test_broadcast_with_no_connections_returns_immediately():
    """无连接时立即返回（不创建任务）."""
    mgr = ConnectionManager()
    mgr._connections = {}
    # 若创建任务则会引入开销；1s 超时足够检测立即返回
    await asyncio.wait_for(mgr.broadcast({"type": "test"}), timeout=1.0)


async def test_broadcast_cleans_up_failed_connections():
    """发送失败的连接应被清理，正常连接保留."""
    mgr = ConnectionManager()
    failed_ws = AsyncMock()
    failed_ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    ok_ws = AsyncMock()

    mgr._connections = {
        "failed": failed_ws,
        "ok": ok_ws,
    }

    await mgr.broadcast({"type": "test"})
    assert "failed" not in mgr._connections
    assert "ok" in mgr._connections
