"""shell_command 子进程清理超时测试

覆盖:
1. 命令超时后 proc.wait() 有 5s 超时保护，防止无限挂起
"""
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


@pytest.mark.asyncio
async def test_shell_command_wait_has_timeout_protection():
    """验证命令超时后，proc.wait() 被 asyncio.wait_for 以 5s 超时保护。

    原缺陷：proc.kill() 后 await proc.wait() 无超时，若子进程不退出则 task 永久挂起。
    """
    from tools.file_tools_v2 import shell_command

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.kill = MagicMock()

    async def _wait_coro():
        return 0

    mock_proc.wait = _wait_coro

    wait_for_calls = []

    async def _tracking_wait_for(coro, timeout):
        wait_for_calls.append(timeout)
        if asyncio.iscoroutine(coro):
            return await coro
        return coro

    with patch("tools.file_tools_v2.asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("tools.file_tools_v2.asyncio.wait_for", _tracking_wait_for):
            result = await shell_command("echo hello")

    # 应有 timeout=5.0 的调用（保护 proc.wait）
    assert 5.0 in wait_for_calls, f"应有 timeout=5.0 的 wait_for 调用，实际: {wait_for_calls}"
    assert result.success is False


@pytest.mark.asyncio
async def test_shell_command_wait_timeout_on_general_error():
    """验证非超时异常路径中，proc.wait() 同样有 5s 超时保护。"""
    from tools.file_tools_v2 import shell_command

    mock_proc = MagicMock()
    # communicate 抛非 TimeoutError 异常
    mock_proc.communicate = AsyncMock(side_effect=OSError("broken pipe"))
    mock_proc.kill = MagicMock()

    async def _wait_coro():
        return 0

    mock_proc.wait = _wait_coro

    wait_for_calls = []

    async def _tracking_wait_for(coro, timeout):
        wait_for_calls.append(timeout)
        if asyncio.iscoroutine(coro):
            return await coro
        return coro

    with patch("tools.file_tools_v2.asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("tools.file_tools_v2.asyncio.wait_for", _tracking_wait_for):
            result = await shell_command("echo hello")

    # 兜底清理路径中也应有 timeout=5.0 的调用
    assert 5.0 in wait_for_calls, f"兜底路径应有 timeout=5.0 的 wait_for 调用，实际: {wait_for_calls}"
    assert result.success is False
