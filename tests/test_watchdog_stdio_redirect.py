"""看门狗子进程 stdio 继承守护测试。

防止 P0 可观测性回归：watchdog 把子进程 stdout/stderr 丢到 DEVNULL，
导致主程序 DEBUG 日志和崩溃 traceback 全部丢失，cmd 窗口只剩看门狗几行。

根因（a449d21 "I4 加固"）：
  subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)
  把主程序 stderr sink（DEBUG 级别日志）和 Python 未捕获异常的
  traceback 全部丢弃，cmd 窗口里主程序日志 90% 消失。

修复：
  stdout/stderr 不重定向，子进程继承父进程控制台，主程序日志实时
  显示到 cmd 窗口（与最开始的版本一致）。主程序自身的文件 sink
  （logs/agent.log）仍独立落盘，互不影响。仅 stdin 用 DEVNULL。

  依据：_should_hide_console() 用 GetConsoleProcessList 判断——watchdog
  模式下主程序与 watchdog 共享控制台（count>1），不会隐藏控制台，
  所以继承父进程 stdio 后日志能正常显示到 cmd。
"""
import subprocess
import sys
from utils.watchdog_runner import Watchdog, DEFAULTS


class _FakeProc:
    def __init__(self):
        self.pid = 12345

    def poll(self):
        return None


def test_start_inherits_stdout_stderr_not_devnull(tmp_path, monkeypatch):
    """子进程 stdout/stderr 必须继承父进程（显示到 cmd），不能是 DEVNULL。

    这是 P0 不变量：DEVNULL 会吃掉主程序 DEBUG 日志和崩溃 traceback，
    导致 cmd 窗口只剩看门狗日志，主程序变成黑箱。
    """
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["stdin"] = kwargs.get("stdin")
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    cfg = dict(DEFAULTS)
    cfg["cmd"] = [sys.executable, "-c", "pass"]
    cfg["log_file"] = str(tmp_path / "watchdog.log")
    wd = Watchdog(cfg["cmd"], str(tmp_path), cfg)

    ok = wd._start()
    assert ok is True

    # stdin 仍 DEVNULL（子进程不需要输入）
    assert captured["stdin"] is subprocess.DEVNULL
    # stdout/stderr 必须不是 DEVNULL（继承父进程，显示到 cmd）
    assert captured["stdout"] is not subprocess.DEVNULL, (
        "stdout=DEVNULL 会吃掉主程序日志，cmd 窗口看不到主程序输出"
    )
    assert captured["stderr"] is not subprocess.DEVNULL, (
        "stderr=DEVNULL 会吃掉崩溃 traceback，诊断时变黑箱"
    )


def test_start_inherits_stdio_even_without_log_file(tmp_path, monkeypatch):
    """无 log_file 时同样继承 stdio（不降级到 DEVNULL）。"""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    cfg = dict(DEFAULTS)
    cfg["cmd"] = [sys.executable, "-c", "pass"]
    cfg["log_file"] = ""
    wd = Watchdog(cfg["cmd"], str(tmp_path), cfg)

    assert wd._start() is True
    # 不传 stdout/stderr（None）= 继承父进程控制台，不是 DEVNULL
    assert captured["stdout"] is not subprocess.DEVNULL
    assert captured["stderr"] is not subprocess.DEVNULL
