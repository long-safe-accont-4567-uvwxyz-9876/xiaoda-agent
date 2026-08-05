"""回归测试：CLI 空闲等待输入时，后台事件循环守护线程仍能响应服务端 keepalive ping。

修复前：CLI 用 `asyncio.run_until_complete` 驱动事件循环，一旦进入 prompt 等待用户输入，
循环不再运行，无法及时回 pong，被 uvicorn 服务端（默认为 20s keepalive）超时关闭，
导致执行斜杠命令时报 "keepalive ping timeout"。

修复后：事件循环由后台守护线程 `run_forever` 持续驱动，WS 操作经
`asyncio.run_coroutine_threadsafe` 提交，空闲期也能响应心跳。
"""
import time

import websockets

import cli
import cli_client


async def _handler(ws):
    try:
        async for _ in ws:
            pass
    except websockets.ConnectionClosed:
        pass


def test_cli_ws_keepalive_survives_idle():
    """后台 loop 驱动下，空闲 6s（服务端 keepalive ping 多次超时周期）连接仍存活。"""
    c = cli.CLIInterface()  # 启动后台 loop 守护线程

    # server 与 client 都挂在 c._loop 上，避免跨事件循环的清理问题
    async def _start_server():
        server = await websockets.serve(
            _handler, "127.0.0.1", 0, ping_interval=2, ping_timeout=2)
        port = server.sockets[0].getsockname()[1]
        return server, port

    server, port = c._run_coro(_start_server())
    try:
        ws = cli_client.WSClient("", host="127.0.0.1", port=port)
        c._run_coro(ws.connect())

        time.sleep(6)  # 空闲：服务端应在 ping_interval=2s 内多次 keepalive 并等 pong

        async def _send_ok():
            await ws._ws.send("keepalive-check")
            return True

        try:
            alive = c._run_coro(_send_ok())
        except Exception:
            alive = False
    finally:
        c._run_coro(ws.close())

        async def _stop():
            server.close()
            await server.wait_closed()

        c._run_coro(_stop())
        c._loop.call_soon_threadsafe(c._loop.stop)
        c._loop_thread.join(timeout=2)

    assert alive, "空闲 6s 后连接被服务端 keepalive 关闭（后台 loop 未响应心跳）"