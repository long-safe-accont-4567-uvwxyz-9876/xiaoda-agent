"""_cleanup_pty 线程安全测试

覆盖:
1. _cleanup_pty 使用 asyncio.run_coroutine_threadsafe 而非 ensure_future(..., loop=loop)
"""
import asyncio
from unittest.mock import patch, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cleanup_pty_uses_run_coroutine_threadsafe():
    """验证 _cleanup_pty 使用 asyncio.run_coroutine_threadsafe 发送终端退出事件。

    原缺陷：使用 asyncio.ensure_future(..., loop=loop)，loop 参数在 Python 3.10+
    已弃用，Python 3.12+ 会抛出 TypeError 导致崩溃。
    """
    from web.ws_hub import _cleanup_pty

    loop = asyncio.get_running_loop()

    mock_loop = MagicMock()
    mock_loop.remove_reader = MagicMock()

    with patch("web.ws_hub._pty_sessions", {
        "test_sid": {
            "pid": 12345,
            "fd": 99,
            "conn_id": "conn_1",
            "shell": "bash",
            "alive": True,
            "loop": mock_loop,
            "is_windows": False,
        }
    }):
        with patch("web.ws_hub.asyncio.run_coroutine_threadsafe") as mock_run:
            with patch("web.ws_hub.os.waitpid", return_value=(12345, 0)):
                with patch("web.ws_hub.os.close"):
                    _cleanup_pty("test_sid")

            mock_run.assert_called_once()
            call_args = mock_run.call_args
            coro = call_args[0][0]
            passed_loop = call_args[0][1]
            assert asyncio.iscoroutine(coro), "应传入 coroutine 对象"
            assert passed_loop is mock_loop, "应传入正确的事件循环"


@pytest.mark.asyncio
async def test_cleanup_pty_handles_runtime_error_gracefully():
    """验证 _cleanup_pty 在 run_coroutine_threadsafe 抛 RuntimeError 时不崩溃。"""
    from web.ws_hub import _cleanup_pty

    mock_loop = MagicMock()
    mock_loop.remove_reader = MagicMock()

    with patch("web.ws_hub._pty_sessions", {
        "test_sid": {
            "pid": 12345,
            "fd": 99,
            "conn_id": "conn_1",
            "shell": "bash",
            "alive": True,
            "loop": mock_loop,
            "is_windows": False,
        }
    }):
        with patch("web.ws_hub.asyncio.run_coroutine_threadsafe", side_effect=RuntimeError("loop closed")):
            with patch("web.ws_hub.os.waitpid", return_value=(12345, 0)):
                with patch("web.ws_hub.os.close"):
                    # 不应抛异常
                    _cleanup_pty("test_sid")
