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


# ── 自动重连：主进程（nahida-web）重启后 WS 连接被 1000 关闭 ────────────
# 根因：CLI 只在启动时连接一次，主进程重启（ExecStartPre fuser -k 8080/tcp
# 杀旧进程）后旧连接被 1000 正常关闭，下次发消息报 "received 1000 (OK)"。
# 修复：_ws_chat_with_reconnect 识别 ConnectionClosed 后自动重连并重试一次。


class _FakeWS:
    """模拟 WSClient：首次 chat 抛 ConnectionClosed，重试后返回回复。"""

    def __init__(self, *, fail_times: int = 1):
        self.calls = 0
        self.fail_times = fail_times
        self.close_calls = 0

    async def chat(self, text: str, status_callback=None) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            # 与服务端重启场景一致：连接被 1000 正常关闭
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        return f"reply-{text}"

    async def close(self) -> None:
        self.close_calls += 1


def _new_cli() -> cli.CLIInterface:
    return cli.CLIInterface()


def _teardown(c: cli.CLIInterface) -> None:
    c._loop.call_soon_threadsafe(c._loop.stop)
    c._loop_thread.join(timeout=2)


def test_is_ws_closed_error_detects_connection_closed():
    """ConnectionClosed* 异常应被识别为连接关闭（触发重连），其他异常不触发。"""
    c = _new_cli()
    try:
        assert c._is_ws_closed_error(
            websockets.exceptions.ConnectionClosedOK(None, None)) is True
        assert c._is_ws_closed_error(
            websockets.exceptions.ConnectionClosedError(None, None)) is True
        assert c._is_ws_closed_error(
            websockets.exceptions.ConnectionClosed(None, None)) is True
        assert c._is_ws_closed_error(RuntimeError("boom")) is False
        assert c._is_ws_closed_error(ValueError("boom")) is False
    finally:
        _teardown(c)


def test_ws_chat_with_reconnect_retries_after_connection_closed(monkeypatch):
    """连接被服务端关闭后，自动重连并重试一次，消息不丢失。"""
    c = _new_cli()
    fake = _FakeWS(fail_times=1)
    c._ws = fake
    reconnected = []

    def _fake_reconnect():
        # 模拟真实 _connect_main_process：成功后重建 _ws（新连接正常）
        reconnected.append(1)
        c._ws = _FakeWS(fail_times=0)
        return True

    monkeypatch.setattr(c, "_connect_main_process", _fake_reconnect)
    try:
        result = c._ws_chat_with_reconnect("你好")
    finally:
        _teardown(c)

    assert fake.calls == 1, f"旧连接应只尝试 1 次即失败，实际 {fake.calls}"
    assert len(reconnected) == 1, "连接关闭后应触发一次重连"
    assert fake.close_calls == 1, "重连前应关闭旧连接"
    assert result == "reply-你好", "重连后应成功拿到回复（消息不丢失）"


def test_ws_chat_with_reconnect_raises_non_ws_errors():
    """非连接关闭类异常（如主进程处理出错）不应触发重连，原样上抛。"""
    c = _new_cli()
    real = RuntimeError("主进程处理出错")

    class _ErrWS:
        async def chat(self, text, status_callback=None):
            raise real

    c._ws = _ErrWS()
    try:
        try:
            c._ws_chat_with_reconnect("hi")
            raise AssertionError("应抛出 RuntimeError")
        except RuntimeError as e:
            assert e is real, "非连接关闭异常应原样上抛"
    finally:
        _teardown(c)


def test_ws_chat_with_reconnect_fails_cleanly_when_reconnect_fails(monkeypatch):
    """重连失败时抛出明确错误，而非 AttributeError/静默吞掉。"""
    c = _new_cli()
    fake = _FakeWS(fail_times=99)
    c._ws = fake
    monkeypatch.setattr(c, "_connect_main_process", lambda: False)
    try:
        try:
            c._ws_chat_with_reconnect("hi")
            raise AssertionError("应抛出 RuntimeError")
        except RuntimeError as e:
            assert "重连失败" in str(e), f"应提示重连失败，实际: {e}"
    finally:
        _teardown(c)