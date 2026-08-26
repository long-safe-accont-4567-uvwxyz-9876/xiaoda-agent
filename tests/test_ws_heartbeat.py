"""G5: WebSocket 心跳测试 - 死连接 40s 内清理.

连接一律通过 register() 构造（心跳任务/写入任务由 register 真实启动），
覆盖"心跳发 ping""发送失败 → unregister 清理"的完整真实路径。
不再手工注入 _connections：那会绕过 register 启动的真实心跳协程与
unregister 自取消防护路径（心跳任务自身调用 unregister 的生产形态）。
"""
import asyncio
import time
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


async def _poll(predicate, timeout=5.0):
    """轮询等待条件成立（清理由后台协程异步完成，无直接 await 点）."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(
                "condition not met within {:.0f}s".format(timeout))
        await _real_sleep(0.01)


async def test_heartbeat_sends_ping_every_30s():
    """register 启动的心跳协程应按间隔发送 ping."""
    mgr = ConnectionManager()
    ws = AsyncMock()
    conn_id = mgr.register(ws)

    # register 必须启动真实心跳任务
    hb = mgr._heartbeat_tasks.get(conn_id)
    assert hb is not None and not hb.done()

    # 加速：patch sleep（不阻塞，仅让出事件循环），让注册的心跳协程跑完一轮
    with patch("web.ws_hub.asyncio.sleep", new=_fast_sleep):
        await _real_sleep(0.1)

    # 应至少发过一次 ping
    sent_events = [call.args[0] for call in ws.send_json.call_args_list]
    assert any(e.get("type") == "ping" for e in sent_events)

    await mgr.unregister(conn_id)
    assert conn_id not in mgr._heartbeat_tasks


async def test_heartbeat_cleans_up_dead_connection():
    """无 pong / 发送失败的连接应被完整清理.

    send 失败走 unregister——且是心跳任务自身调用 unregister 的生产形态：
    修复后不得自取消（否则清理中断、连接残留）；正常完成后心跳任务应
    正常结束而非 cancelled。
    """
    mgr = ConnectionManager()
    ws = AsyncMock()
    ws.send_json = AsyncMock(side_effect=RuntimeError("dead"))
    conn_id = mgr.register(ws)
    hb = mgr._heartbeat_tasks[conn_id]

    with patch("web.ws_hub.asyncio.sleep", new=_fast_sleep):
        await _poll(lambda: conn_id not in mgr._connections)

    # 死连接应被清理
    assert conn_id not in mgr._connections
    assert conn_id not in mgr._heartbeat_tasks
    assert conn_id not in mgr._pong_events
    # 心跳任务是自身走完 unregister 后正常 return，而非被自取消打断
    await _poll(lambda: hb.done())
    assert not hb.cancelled(), (
        "心跳任务在 unregister 中被自取消 —— 清理中断会导致连接状态半残")
