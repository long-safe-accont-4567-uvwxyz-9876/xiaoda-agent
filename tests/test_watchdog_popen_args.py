"""看门狗 spawn 主进程的命令行参数回归测试。

防止 P0 bug 回归：v0.5.62 曾用
    Popen(list(cmd[1:]), executable=cmd[0], ...)
想规避安装目录含空格（"D:\\Xiaoda Agent\\..."），但 Windows 上
subprocess 提供 executable= 时，lpApplicationName 用 executable、
lpCommandLine 只由 args 生成（Python 文档明确），即命令行变成
"--desktop --host ..." 不含 exe 路径。PyInstaller bootloader 从
lpCommandLine 解析 sys.argv 时把第一个 token "--desktop" 当作
argv[0]（程序名），argparse 收到的 sys.argv[1:] 丢掉 --desktop →
主进程不进入 desktop 模式，反而再次走看门狗分支嵌套启动 → 进程
爆炸/端口争抢/无限重启（双击无法启动；0.5.59 正常、0.5.62 异常、
手动 --desktop 正常）。
"""
import logging
import subprocess

from utils.watchdog_runner import DEFAULTS, Watchdog


def _silent_log():
    log = logging.getLogger("watchdog.test")
    log.handlers = []
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


def _make_watchdog(cmd):
    cfg = dict(DEFAULTS)
    wd = Watchdog(cmd, r"D:\Xiaoda Agent", cfg)
    wd.log = _silent_log()
    return wd


def test_start_popen_receives_full_cmd():
    """回归：_start() 必须把完整 cmd（含 exe 路径）传给 Popen 的 args，
    禁止拆分 executable=（否则 Windows 上 --desktop 被 bootloader 吞掉）。"""
    cmd = [r"D:\Xiaoda Agent\xiaoda-agent.exe", "--desktop", "--host", "0.0.0.0", "--port", "8082"]
    wd = _make_watchdog(cmd)

    captured = {}

    class _FakeProc:
        pid = 99999

    real_popen = subprocess.Popen

    def _fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    subprocess.Popen = _fake_popen
    try:
        ok = wd._start()
    finally:
        subprocess.Popen = real_popen

    assert ok
    assert "args" in captured, "Popen 必须被调用"
    # 核心约束：args[0] 必须是 exe 完整路径（list2cmdline 会自动加引号）
    assert captured["args"][0] == cmd, (
        f"Popen 必须收到完整 cmd（含 exe 路径），实际: {captured['args'][0]!r}"
    )
    # 禁止分离 executable=（v0.5.62 bug：--desktop 被 bootloader 当 argv[0] 吞掉）
    assert "executable" not in captured["kwargs"], (
        "禁止 Popen(executable=cmd[0]) 拆分，否则 lpCommandLine 不含 exe 路径"
    )


def test_list2cmdline_quotes_exe_path_with_spaces():
    """cmd 是 list 时，list2cmdline 自动给带空格的 exe 路径加引号，
    CreateProcess 可正确解析——无需 executable= 分离即可支持空格目录。"""
    cmd = [r"D:\Xiaoda Agent\xiaoda-agent.exe", "--desktop"]
    cl = subprocess.list2cmdline(cmd)
    assert cl.startswith(r'"D:\Xiaoda Agent\xiaoda-agent.exe"'), cl
    assert "--desktop" in cl

    # 对照：v0.5.62 拆分方式的 lpCommandLine 以 --desktop 开头 → argv[0] 错位
    cl_split = subprocess.list2cmdline(cmd[1:])
    assert cl_split == "--desktop"


def test_build_cmd_keeps_mode_flag():
    """build_watchdog_config 构造的 cmd 必须包含 --desktop 模式参数。"""
    import argparse

    from utils.watchdog_runner import build_watchdog_config

    p = argparse.ArgumentParser(prog="watchdog")
    p.add_argument("--port", type=int, default=8082)
    p.add_argument("--host", type=str, default="127.0.0.1")
    p.add_argument("--mode", choices=["web", "desktop"], default="web")
    p.add_argument("--check-interval", type=int, default=DEFAULTS["check_interval"])
    p.add_argument("--freeze-threshold", type=int, default=DEFAULTS["freeze_threshold"])
    p.add_argument("--max-restarts", type=int, default=DEFAULTS["max_restarts"])
    p.add_argument("--ping-retries", type=int, default=DEFAULTS["ping_retries"])
    p.add_argument("--log-file", type=str, default="")
    args = p.parse_args(["--host", "0.0.0.0", "--port", "8082", "--mode", "desktop"])
    cfg = build_watchdog_config(args)

    assert "--desktop" in cfg["cmd"]
    assert "--host" in cfg["cmd"]
    assert "0.0.0.0" in cfg["cmd"]
