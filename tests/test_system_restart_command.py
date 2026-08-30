"""/system/restart 重启命令构造回归测试。

原缺陷（2026-08-30 修复）：PyInstaller frozen 下 sys.executable 与 argv[0]
同为 exe 路径，旧拼法生成 "exe exe --web ..."——exe 路径被当模式参数，
argparse 无法识别直接退出，WebUI 的重启按钮在打包版上静默失效；
且参数用 POSIX shlex.quote 拼接，对 cmd.exe 语义不正确。

修复后命令改为列表构造（_restart_command_list），再按平台拼接
（_format_restart_command：Windows 用 subprocess.list2cmdline，
POSIX 用 shlex.join），两步均可独立测试。
"""
import os
import shlex
import subprocess
import sys
import types

from utils.common import DEFAULT_WEBUI_PORT
from web.routers import system as system_router
from web.routers.system import _format_restart_command, _restart_command_list


def _patch_router_os_name(monkeypatch, name: str) -> None:
    """只替换 web.routers.system 内看到的 os 引用，禁止改全局 os.name——
    Python 3.11 的 pathlib.Path.__new__ 动态读 os.name，全局改成 'nt' 会让
    pytest 自身在失败报告路径上实例化 WindowsPath 而 INTERNALERROR。"""
    monkeypatch.setattr(system_router, "os", types.SimpleNamespace(name=name))


def test_frozen_restart_command_has_no_duplicated_executable():
    """frozen 下命令必须是 [exe, *args]——argv[0]（exe 路径）不得再当参数。"""
    argv = [r"C:\App\Xiaoda Agent\xiaoda-agent.exe",
            "--desktop", "--host", "127.0.0.1", "--port", "8082"]
    cmd = _restart_command_list(argv, frozen=True)

    assert cmd[0] == sys.executable
    # 核心回归：exe 路径不能重复出现（旧 bug 生成 "exe exe --web ..."）
    assert r"xiaoda-agent.exe" not in cmd[1:]
    assert cmd[1:] == ["--desktop", "--host", "127.0.0.1", "--port", "8082"]


def test_source_restart_command_is_python_plus_script():
    """源码模式保持 [python, script, *args]，script 取 argv[0] 绝对路径。"""
    argv = ["/opt/xiaoda-agent/agent.py", "--web", "--host", "0.0.0.0", "--port", "8082"]
    cmd = _restart_command_list(argv, frozen=False)

    assert cmd[0] == sys.executable
    assert cmd[1] == os.path.abspath("/opt/xiaoda-agent/agent.py")
    assert cmd[2:] == ["--web", "--host", "0.0.0.0", "--port", "8082"]


def test_restart_command_falls_back_to_web_defaults_without_args():
    """argv 只有程序名（无模式参数，如双击 exe）时回退默认 Web 启动参数。"""
    expected_args = ["--web", "--host", "0.0.0.0", "--port", str(DEFAULT_WEBUI_PORT)]

    frozen_cmd = _restart_command_list(["xiaoda-agent.exe"], frozen=True)
    assert frozen_cmd == [sys.executable, *expected_args]

    source_cmd = _restart_command_list(["agent.py"], frozen=False)
    assert source_cmd == [sys.executable, os.path.abspath("agent.py"), *expected_args]


def test_format_restart_command_quotes_spaces_on_windows(monkeypatch):
    """Windows 侧用 list2cmdline：含空格的路径必须整体加引号。"""
    _patch_router_os_name(monkeypatch, "nt")
    cmd = [r"C:\App\Xiaoda Agent\xiaoda-agent.exe", "--web", "--host", "0.0.0.0"]
    joined = _format_restart_command(cmd)

    assert joined == subprocess.list2cmdline(cmd)
    assert joined.startswith('"C:\\App\\Xiaoda Agent\\xiaoda-agent.exe"')
    assert "--web" in joined


def test_format_restart_command_posix_uses_shlex_join(monkeypatch):
    """POSIX 侧用 shlex.join：含空格路径同样被引号保护。"""
    _patch_router_os_name(monkeypatch, "posix")
    cmd = ["/usr/bin/python3", "/opt/xi ao da/agent.py", "--web"]
    joined = _format_restart_command(cmd)

    assert joined == shlex.join(cmd)
    assert "'/opt/xi ao da/agent.py'" in joined


def test_frozen_restart_command_roundtrip_with_spaces(monkeypatch):
    """端到端：frozen（exe 路径含空格）经列表构造 + list2cmdline 后，
    exe 只出现一次且被引号包裹、参数逐个保留（模拟 Windows os.name）。"""
    argv = [r"C:\Users\u\AppData\Local\Xiaoda Agent\xiaoda-agent.exe",
            "--web", "--host", "127.0.0.1", "--port", "8082"]
    # frozen 下 exe 取 sys.executable（生产环境即打包 exe 本体）；
    # 测试注入含空格路径以验证 list2cmdline 的引号保护
    monkeypatch.setattr(sys, "executable",
                        r"C:\Users\u\AppData\Local\Xiaoda Agent\xiaoda-agent.exe")
    _patch_router_os_name(monkeypatch, "nt")
    cmd = _restart_command_list(argv, frozen=True)
    joined = _format_restart_command(cmd)

    assert joined.count("xiaoda-agent.exe") == 1
    assert joined.startswith('"C:\\Users\\u\\AppData\\Local\\Xiaoda Agent\\xiaoda-agent.exe"')
    # 参数必须逐个保留（旧 bug 在 exe 后丢失第一个真实参数）
    for token in ("--web", "--host", "127.0.0.1", "--port", "8082"):
        assert token in joined
