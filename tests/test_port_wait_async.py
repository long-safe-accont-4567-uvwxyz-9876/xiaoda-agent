"""Task 9: 端口冲突检测异步化测试。

验证：
1. 端口可用时，_wait_for_port_available_async 立即返回，不调用 asyncio.sleep
2. 端口被占用时，异步等待调用 asyncio.sleep 重试（而非 time.sleep）
3. 桌面模式同步 _wait_for_port_available 使用 0.5s 间隔（而非 2s）
"""
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import agent


@pytest.mark.asyncio
async def test_async_port_wait_returns_immediately_when_port_available():
    """端口可用时，异步等待立即返回，不调用 asyncio.sleep。"""
    with patch("socket.socket") as mock_socket_cls:
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        # bind 不抛异常 = 端口可用

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await agent._wait_for_port_available_async("0.0.0.0", 9999)
            mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_async_port_wait_retries_on_oserror():
    """端口被占用时，异步等待调用 asyncio.sleep 重试。"""
    with patch("socket.socket") as mock_socket_cls:
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = [OSError("in use"), None]  # 第1次失败，第2次成功
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await agent._wait_for_port_available_async("0.0.0.0", 9999)
            mock_sleep.assert_called_once_with(2)


def test_sync_port_wait_uses_half_second_interval():
    """桌面模式同步等待使用 0.5s 间隔（而非 2s）。"""
    with patch("socket.socket") as mock_socket_cls:
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = [OSError("in use"), None]
        mock_socket_cls.return_value.__enter__.return_value = mock_sock

        with patch("time.sleep") as mock_sleep:
            agent._wait_for_port_available("0.0.0.0", 9999)
            mock_sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
