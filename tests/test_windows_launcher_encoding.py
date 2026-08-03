from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"


def _assert_ascii(name: str) -> None:
    data = (SCRIPTS / name).read_bytes()
    assert data == data.decode("ascii").encode("ascii"), f"{name} contains non-ASCII bytes"


def test_start_windows_bat_is_ascii_only():
    _assert_ascii("start-windows.bat")


def test_doctor_bat_is_ascii_only():
    _assert_ascii("doctor.bat")


def test_auto_update_bat_is_ascii_only():
    _assert_ascii("auto-update.bat")


def test_windows_launcher_keeps_watchdog_launch_contract():
    launcher = (SCRIPTS / "start-windows.bat").read_text(encoding="ascii")

    assert 'set "WEBUI_PORT=8082"' in launcher
    assert '"%EXE_PATH%" watchdog --mode %LAUNCH_MODE:--=% --port %WEBUI_PORT%' in launcher
    assert 'if /i "%~1"=="--desktop" goto :check_desktop' in launcher


def test_doctor_bat_keeps_exe_fallback_contract():
    doctor = (SCRIPTS / "doctor.bat").read_text(encoding="ascii")

    assert '"!EXE_PATH!" doctor !ARGS!' in doctor
    assert 'where python >nul 2>nul' in doctor


def test_auto_update_bat_keeps_ps1_delegation_contract():
    au = (SCRIPTS / "auto-update.bat").read_text(encoding="ascii")

    assert 'set "REPO=long-safe-accont-4567-uvwxyz-9876/xiaoda-agent"' in au
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_PATH%"' in au
