"""P1-5 测试: Windows 控制台隐藏不应误杀父进程。

Bug: _run_desktop 在 win32 下无条件调用 GetConsoleWindow + ShowWindow(SW_HIDE)，
如果用户从 cmd.exe / 批处理脚本启动 agent.py --desktop，会把 cmd.exe 的控制台
也隐藏掉，导致用户失去对父终端的控制。

修复目标:
1. 抽出 _should_hide_console() 函数，仅当控制台为本进程独占时才隐藏
2. 使用 GetConsoleProcessList 检查附加到控制台的进程数
3. 若附加进程 > 1，说明与父进程共享，不隐藏
"""
import sys
from unittest.mock import MagicMock, patch

import pytest


def _skip_if_not_windows():
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")


def test_should_hide_console_function_exists():
    """应有 _should_hide_console 可测函数。"""
    import agent
    assert hasattr(agent, "_should_hide_console"), "缺少 _should_hide_console 函数"


def test_should_hide_when_only_self_attached():
    """控制台只有本进程附加时（双击启动），应返回 True（可隐藏）。"""
    _skip_if_not_windows()
    import agent

    with patch("ctypes.windll.kernel32.GetConsoleProcessList") as mock_list:
        # GetConsoleProcessList(buf, buf_size) returns count of processes attached
        # 我们让返回 1（只有本进程）
        def _side_effect(buf, size):
            buf[0] = 1234  # 本进程 pid
            return 1
        mock_list.side_effect = _side_effect

        assert agent._should_hide_console() is True


def test_should_not_hide_when_shared_with_parent():
    """控制台与父进程（cmd.exe）共享时，不应隐藏（避免误杀父终端）。"""
    _skip_if_not_windows()
    import agent

    with patch("ctypes.windll.kernel32.GetConsoleProcessList") as mock_list:
        # 返回 2 表示有 2 个进程附加（本进程 + 父进程 cmd.exe）
        def _side_effect(buf, size):
            buf[0] = 1234  # 本进程
            buf[1] = 5678  # cmd.exe
            return 2
        mock_list.side_effect = _side_effect

        assert agent._should_hide_console() is False, \
            "与父进程共享控制台时不应隐藏"


def test_should_not_hide_when_no_console():
    """无控制台（已 pythonw.exe 启动）时不应尝试隐藏。"""
    _skip_if_not_windows()
    import agent

    with patch("ctypes.windll.kernel32.GetConsoleWindow", return_value=0):
        assert agent._should_hide_console() is False


def test_should_hide_console_handles_ctypes_error():
    """ctypes 调用失败时应安全降级（不隐藏）。"""
    _skip_if_not_windows()
    import agent

    with patch("ctypes.windll.kernel32.GetConsoleWindow", side_effect=OSError("denied")):
        # 不应抛错，应安全返回 False
        assert agent._should_hide_console() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
