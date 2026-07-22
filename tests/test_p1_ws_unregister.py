"""P1-4 测试: unregister 应异步关闭 WebSocket。

Bug: unregister 是同步方法，仅清理内部 dict，不调用 ws.close()，
导致死连接的底层 TCP 资源不能及时释放（需要等 GC 或客户端断开）。

修复目标:
1. unregister 改为 async 方法
2. 在清理内部状态前 await ws.close()
3. 所有调用点（heartbeat_loop、send_to、broadcast、_safe_send、websocket_endpoint finally）
   都 await 它
"""
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
    """CodeRabbit F6: unregister 应先 await ws.close() 再清理内部状态。

    用 side_effect 在 close 调用瞬间断言 conn_id 仍在 _connections 中，
    证明关闭顺序正确（先释放 TCP 资源，再清理 dict，避免死连接残留）。
    若实现先 pop 再 close，本断言会失败。
    """
    mgr = _make_manager()
    ws = _make_ws()
    conn_id = mgr.register(ws)

    close_seen_state = {"in_connections": None}

    async def _assert_state_during_close():
        # close 调用瞬间 conn_id 应仍在 _connections 中（先 close 再 pop）
        close_seen_state["in_connections"] = conn_id in mgr._connections

    ws.close = AsyncMock(side_effect=_assert_state_during_close)

    await mgr.unregister(conn_id)

    ws.close.assert_awaited(), "unregister 未调用 ws.close()"
    assert close_seen_state["in_connections"] is True, (
        "ws.close() 调用时 conn_id 已不在 _connections 中 —— "
        "违反 '先 close 再 pop' 顺序，可能导致死连接 TCP 资源泄漏"
    )


@pytest.mark.asyncio
async def test_unregister_handles_close_error():
    """CodeRabbit F7: ws.close() 抛错时不应影响内部状态清理（defensive）。

    填充全部 5 个 per-connection store 后再注销，验证 close 抛错后
    所有 store 仍被清理（避免只清理部分 store 的 bug 被空 dict 掩盖）。
    """
    mgr = _make_manager()
    ws = _make_ws()
    ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
    conn_id = mgr.register(ws)

    # CodeRabbit F7: 预断言全部 5 个 store 已填充，确保 not in 断言非空真
    assert conn_id in mgr._connections, "register 未填充 _connections"
    assert conn_id in mgr._agent_map, "register 未填充 _agent_map"
    assert conn_id in mgr._session_map, "register 未填充 _session_map"
    assert conn_id in mgr._heartbeat_tasks, "register 未填充 _heartbeat_tasks"
    assert conn_id in mgr._pong_events, "register 未填充 _pong_events"

    # 不应抛错
    await mgr.unregister(conn_id)

    # 内部状态仍应清理（全部 5 个 store）
    assert conn_id not in mgr._connections
    assert conn_id not in mgr._agent_map
    assert conn_id not in mgr._session_map
    assert conn_id not in mgr._heartbeat_tasks
    assert conn_id not in mgr._pong_events


@pytest.mark.asyncio
async def test_unregister_clears_internal_state():
    """CodeRabbit F7: unregister 应清理全部 5 个内部状态。

    显式预断言 register 已填充全部 5 个 store，再验证注销后均被清理。
    否则 not in 断言对未填充的 store 是空真（掩盖 register 漏填 bug）。
    """
    mgr = _make_manager()
    ws = _make_ws()
    conn_id = mgr.register(ws)

    # CodeRabbit F7: 预断言 —— 确认 register 已填充全部 5 个 store
    assert conn_id in mgr._connections, "register 未填充 _connections"
    assert conn_id in mgr._agent_map, "register 未填充 _agent_map"
    assert conn_id in mgr._session_map, "register 未填充 _session_map"
    assert conn_id in mgr._heartbeat_tasks, "register 未填充 _heartbeat_tasks"
    assert conn_id in mgr._pong_events, "register 未填充 _pong_events"

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
