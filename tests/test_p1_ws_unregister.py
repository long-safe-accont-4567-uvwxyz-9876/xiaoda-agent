"""P1-4 测试: unregister 应异步关闭 WebSocket。

Bug: unregister 是同步方法，仅清理内部 dict，不调用 ws.close()，
导致死连接的底层 TCP 资源不能及时释放（需要等 GC 或客户端断开）。

修复目标:
1. unregister 改为 async 方法
2. 在清理内部状态前 await ws.close()
3. 所有调用点（heartbeat_loop、send_to、broadcast、_safe_send、websocket_endpoint finally）
   都 await 它
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_ws():
    """构造 mock WebSocket。"""
    ws = MagicMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.client_state = MagicMock()
    return ws


def _make_manager():
    from web.ws_hub import ConnectionManager
    mgr = ConnectionManager()
    return mgr


@pytest.mark.asyncio
async def test_unregister_is_async():
    """unregister 应是 async 方法（可 await）。"""
    import inspect
    from web.ws_hub import ConnectionManager
    assert inspect.iscoroutinefunction(ConnectionManager.unregister), \
        "unregister 应为 async 方法"


@pytest.mark.asyncio
async def test_unregister_closes_websocket():
    """unregister 应调用 ws.close() 释放底层资源。"""
    mgr = _make_manager()
    ws = _make_ws()
    conn_id = mgr.register(ws)

    await mgr.unregister(conn_id)

    ws.close.assert_awaited(), "unregister 未调用 ws.close()"


@pytest.mark.asyncio
async def test_unregister_handles_close_error():
    """ws.close() 抛错时不应影响内部状态清理（defensive）。"""
    mgr = _make_manager()
    ws = _make_ws()
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
    conn_id = mgr.register(ws)

    # 不应抛错
    await mgr.unregister(conn_id)

    # 内部状态仍应清理
    assert conn_id not in mgr._connections
    assert conn_id not in mgr._agent_map


@pytest.mark.asyncio
async def test_unregister_clears_internal_state():
    """unregister 应清理所有内部状态（dict/heartbeat task/pong event）。"""
    mgr = _make_manager()
    ws = _make_ws()
    conn_id = mgr.register(ws)

    await mgr.unregister(conn_id)

    assert conn_id not in mgr._connections
    assert conn_id not in mgr._agent_map
    assert conn_id not in mgr._session_map
    assert conn_id not in mgr._heartbeat_tasks
    assert conn_id not in mgr._pong_events


@pytest.mark.asyncio
async def test_unregister_idempotent():
    """重复 unregister 不应抛错。"""
    mgr = _make_manager()
    ws = _make_ws()
    conn_id = mgr.register(ws)

    await mgr.unregister(conn_id)
    # 第二次调用不应抛错
    await mgr.unregister(conn_id)
    await mgr.unregister("nonexistent")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
