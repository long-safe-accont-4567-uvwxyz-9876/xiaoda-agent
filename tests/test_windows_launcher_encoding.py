from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"
ROOT = Path(__file__).parents[1]


def _assert_ascii(name: str) -> None:
    data = (SCRIPTS / name).read_bytes()
    assert data == data.decode("ascii").encode("ascii"), f"{name} contains non-ASCII bytes"


def test_doctor_bat_is_ascii_only():
    _assert_ascii("doctor.bat")


def test_auto_update_bat_is_ascii_only():
    _assert_ascii("auto-update.bat")


def test_exe_has_builtin_watchdog_launch_contract():
    """新设计：看门狗契约内置于 exe（agent.py），不再依赖 start-windows.bat 包装层。
    exe 双击桌面窗口时自动进入看门狗模式；watchdog 子命令供外部调用。"""
    agent_main = (ROOT / "agent.py").read_text(encoding="utf-8")

    # watchdog 子命令存在
    assert 'subparsers.add_parser("watchdog"' in agent_main
    assert '--mode", choices=["web", "desktop"]' in agent_main
    # 双击 exe（无子命令）进入桌面窗口时，也应走看门狗
    assert "_should_watchdog_software_window" in agent_main
    assert "run_watchdog_cli" in agent_main


def test_doctor_bat_keeps_exe_fallback_contract():
    doctor = (SCRIPTS / "doctor.bat").read_text(encoding="ascii")

    assert '"!EXE_PATH!" doctor !ARGS!' in doctor
    assert 'where python >nul 2>nul' in doctor


def test_auto_update_bat_keeps_ps1_delegation_contract():
    au = (SCRIPTS / "auto-update.bat").read_text(encoding="ascii")

    assert 'set "REPO=long-safe-accont-4567-uvwxyz-9876/xiaoda-agent"' in au
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_PATH%"' in au
