"""看门狗恢复（重启）逻辑单元测试。

覆盖 watchdog 自动恢复的核心路径：
- 滚动窗口内的重启限流（超过 max_restarts 停止自动恢复）
- 重启窗口过期后允许恢复
- 端口释放等待（空闲即返回 / 占用超时返回 False）
- 无 psutil 时的进程树清理 fallback
- 单实例锁专用退出码常量
"""
import logging

from utils import watchdog_runner
from utils.watchdog_runner import Watchdog, DEFAULTS, _EXIT_ALREADY_RUNNING


def _silent_log():
    log = logging.getLogger("watchdog.test")
    log.handlers = []
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


class _FakeProc:
    """模拟一个仍在运行的子进程。"""

    pid = 12345

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def _make_watchdog(monkeypatch, max_restarts=2):
    """构造最小化 Watchdog，打桩所有副作用，聚焦恢复逻辑本身。"""
    # 打桩：不真正杀进程 / 不真正等端口 / 不写崩溃快照 / 不真正 sleep / 启动"成功"
    monkeypatch.setattr(watchdog_runner, "_kill_proc_tree", lambda *a, **k: None)
    monkeypatch.setattr(watchdog_runner, "_wait_port_release", lambda *a, **k: True)
    monkeypatch.setattr(watchdog_runner, "_save_crash_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(watchdog_runner.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(Watchdog, "_start", lambda self: True)

    cfg = dict(DEFAULTS)
    cfg["max_restarts"] = max_restarts
    cfg["restart_window"] = 600
    cfg["restart_delay"] = 0
    cfg["start_fail_backoff"] = 0

    wd = Watchdog(["python", "agent.py", "--web"], "/tmp", cfg)
    wd.log = _silent_log()
    wd._proc = _FakeProc()
    return wd


def test_restart_stops_after_max_restarts(monkeypatch):
    """重启次数达到 max_restarts 后应停止自动恢复（返回 False）。"""
    wd = _make_watchdog(monkeypatch, max_restarts=2)

    assert wd._restart("freeze") is True   # 第 1 次
    assert wd._restart("freeze") is True   # 第 2 次
    assert wd._restart("freeze") is False  # 第 3 次：超限，拒绝
    assert len(wd._restart_history) == 2   # 历史不再增长


def test_restart_window_expiry_allows_recovery(monkeypatch):
    """重启窗口过期后，旧记录应被清理，允许再次恢复。"""
    wd = _make_watchdog(monkeypatch, max_restarts=2)

    assert wd._restart("freeze") is True
    assert wd._restart("freeze") is True
    assert wd._restart("freeze") is False  # 已超限

    # 把历史时间戳回拨到窗口之外（601 秒前）
    wd._restart_history = [t - 601 for t in wd._restart_history]
    assert wd._restart("freeze") is True   # 旧记录被清理，恢复可用


def test_restart_records_history(monkeypatch):
    """每次成功重启都应追加一条历史记录。"""
    wd = _make_watchdog(monkeypatch, max_restarts=5)
    assert wd._restart("proc_exited") is True
    assert wd._restart("proc_exited") is True
    assert len(wd._restart_history) == 2


def test_wait_port_release_returns_true_when_free():
    """端口空闲时，_wait_port_release 应立即返回 True。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    assert watchdog_runner._wait_port_release("127.0.0.1", port, timeout=2) is True


def test_wait_port_release_times_out_when_occupied(monkeypatch):
    """端口持续被占用时，_wait_port_release 超时应返回 False。"""

    class _OccupiedSock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0  # 0 = 连接成功 = 端口仍被占用

    monkeypatch.setattr(watchdog_runner.socket, "socket", _OccupiedSock)
    # time 序列：start=0，第一次 while 条件 time.time()=0.5（进入循环），
    # 第二次 while 条件 time.time()=1.5（超时退出）
    times = iter([0.0, 0.5, 1.5])
    monkeypatch.setattr(watchdog_runner.time, "time", lambda: next(times))
    monkeypatch.setattr(watchdog_runner.time, "sleep", lambda *a, **k: None)

    assert watchdog_runner._wait_port_release("127.0.0.1", 8082, timeout=1) is False


def test_wait_port_release_zero_timeout_returns_false(monkeypatch):
    """边界：timeout=0 时，即使端口被占用，也应立即返回 False 且不进入等待循环。"""
    connect_calls = []

    class _OccupiedSock:
        def __init__(self, *a, **k):
            connect_calls.append(a)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 0  # 0 = 端口仍被占用

    monkeypatch.setattr(watchdog_runner.socket, "socket", _OccupiedSock)
    # start=0，while 条件 time.time()=0 → 0 < 0 为 False，直接跳过循环体
    times = iter([0.0, 0.0])
    monkeypatch.setattr(watchdog_runner.time, "time", lambda: next(times))
    monkeypatch.setattr(watchdog_runner.time, "sleep", lambda *a, **k: None)

    assert watchdog_runner._wait_port_release("127.0.0.1", 8082, timeout=0) is False
    assert connect_calls == []  # 循环体一次都未执行


def test_wait_port_release_socket_error_returns_true(monkeypatch):
    """边界：socket 检测抛异常时，应假设端口已释放（返回 True），避免卡住重启。"""

    class _ErrorSock:
        def __init__(self, *a, **k):
            raise OSError("socket create failed")

    monkeypatch.setattr(watchdog_runner.socket, "socket", _ErrorSock)
    times = iter([0.0, 0.5])
    monkeypatch.setattr(watchdog_runner.time, "time", lambda: next(times))
    monkeypatch.setattr(watchdog_runner.time, "sleep", lambda *a, **k: None)

    assert watchdog_runner._wait_port_release("127.0.0.1", 8082, timeout=1) is True


def test_kill_proc_tree_fallback_without_psutil(monkeypatch):
    """无 psutil 时，Linux 上应回退到 os.kill(pid, SIGKILL)。"""
    monkeypatch.setattr(watchdog_runner, "_HAS_PSUTIL", False)
    monkeypatch.setattr(watchdog_runner.sys, "platform", "linux")

    killed = []
    monkeypatch.setattr(
        watchdog_runner.os, "kill", lambda pid, sig: killed.append((pid, sig))
    )

    watchdog_runner._kill_proc_tree(12345, _silent_log())
    assert killed == [(12345, watchdog_runner.signal.SIGKILL)]


def test_run_exits_on_already_running(monkeypatch):
    """子进程以单实例锁退出码 77 退出时，watchdog 应停止重启并正常退出。

    回归：77 是"已有实例在运行"的专用退出码，watchdog 识别后必须 break，
    否则会无限重启（与已存在实例争抢端口）。
    """

    class _Exited77:
        pid = 12345

        def poll(self):
            return 77

        def wait(self, timeout=None):
            return 77

    wd = _make_watchdog(monkeypatch)
    wd._proc = _Exited77()

    assert wd.run() == 0


def test_exit_already_running_constant():
    """单实例锁专用退出码必须保持 77，与 agent.py 一致。"""
    assert _EXIT_ALREADY_RUNNING == 77
