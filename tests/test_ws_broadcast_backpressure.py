"""WS broadcast 背压测试 - 慢连接不阻塞快连接.

架构说明（33d8f8b 治本修复）：broadcast/send_to 不再直接 await ws.send_json，
改为非阻塞入队（每连接有界队列）+ 独立后台写入任务（_writer_loop）串行发送。
慢/挂起连接的写入不再阻塞调用方（工具执行路径），快连接由各自 writer 任务异步送出。
测试必须通过 register() 注册连接（初始化发送队列与写入任务），并轮询等待
writer 任务异步执行结果。
"""
import asyncio
import time
from unittest.mock import AsyncMock

from web.ws_hub import ConnectionManager


async def _poll(predicate, timeout=3.0):
    """轮询等待条件成立（writer 任务异步执行，无直接 await 点）."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met within {:.0f}s".format(timeout))
        await asyncio.sleep(0.01)


async def test_broadcast_does_not_block_on_slow_connection():
    """1 个慢连接不应阻塞其他连接.

    广播入队即返回（不阻塞调用方）；快连接由各自 writer 任务异步收到事件，
    慢连接的 writer 卡在发送中，不影响快连接。
    """
    mgr = ConnectionManager()

    # 模拟 3 个连接：1 慢 2 快
    slow_ws = AsyncMock()

    async def slow_send(*a, **kw):
        await asyncio.sleep(10)  # 模拟慢连接
    slow_ws.send_json = slow_send

    fast_ws1 = AsyncMock()
    fast_ws2 = AsyncMock()

    cid_slow = mgr.register(slow_ws)
    cid_fast1 = mgr.register(fast_ws1)
    cid_fast2 = mgr.register(fast_ws2)

    # 广播事件：入队即返回，1s 保护足以证明不阻塞
    event = {"type": "test"}
    await asyncio.wait_for(mgr.broadcast(event), timeout=1.0)

    # 快连接最终应收到事件（各自 writer 任务异步发送）
    await _poll(
        lambda: fast_ws1.send_json.call_count >= 1
        and fast_ws2.send_json.call_count >= 1,
        timeout=5.0,
    )
    fast_ws1.send_json.assert_called_with(event)
    fast_ws2.send_json.assert_called_with(event)

    # 清理：慢连接 writer 仍卡在 sleep(10)，取消任务避免泄漏
    await mgr.unregister(cid_slow)
    await mgr.unregister(cid_fast1)
    await mgr.unregister(cid_fast2)


async def test_broadcast_with_no_connections_returns_immediately():
    """无连接时立即返回（不创建任务）."""
    mgr = ConnectionManager()
    # 若创建任务则会引入开销；1s 超时足够检测立即返回
    await asyncio.wait_for(mgr.broadcast({"type": "test"}), timeout=1.0)


async def test_broadcast_cleans_up_failed_connections():
    """发送失败的连接应被清理，正常连接保留."""
    mgr = ConnectionManager()
    failed_ws = AsyncMock()
    failed_ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    ok_ws = AsyncMock()

    cid_failed = mgr.register(failed_ws)
    cid_ok = mgr.register(ok_ws)

    await mgr.broadcast({"type": "test"})

    # failed 连接的 writer 发送失败 → 自动 unregister；ok 连接保留
    await _poll(lambda: cid_failed not in mgr._connections, timeout=5.0)
    assert cid_failed not in mgr._connections
    assert cid_ok in mgr._connections

    await mgr.unregister(cid_ok)
