from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_project_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_windows_package_uses_launcher_for_shortcuts():
    installer = read_project_file("scripts/installer.nsi")

    assert 'CreateShortCut "$DESKTOP\\小妲Agent.lnk" "$INSTDIR\\start-windows.bat" "--desktop"' in installer
    assert 'CreateShortCut "$SMPROGRAMS\\${PRODUCT_NAME}\\小妲Agent.lnk" "$INSTDIR\\start-windows.bat" "--desktop"' in installer


def test_all_windows_packaging_paths_include_update_ps1():
    spec = read_project_file("xiaoda-agent.spec")
    local_release = read_project_file("scripts/build-release.sh")
    workflow = read_project_file(".github/workflows/build-release.yml")

    assert "auto-update.ps1" in spec
    assert "auto-update.ps1" in local_release
    assert "auto-update.ps1" in workflow


def test_ci_does_not_enable_auto_update_by_default():
    workflow = read_project_file(".github/workflows/build-release.yml")

    assert 'New-Item -Path "dist/xiaoda-agent/.auto_update"' not in workflow


def test_uninstall_command_is_quoted():
    installer = read_project_file("scripts/installer.nsi")

    assert '"UninstallString" \'"$INSTDIR\\uninstall.exe"\'' in installer


def test_doctor_uses_delayed_errorlevel_for_py_launcher():
    doctor = read_project_file("scripts/doctor.bat")

    assert "if !errorlevel! equ 0" in doctor


def test_update_checks_tar_exit_code_before_installing():
    updater = read_project_file("scripts/auto-update.ps1")

    assert "$LASTEXITCODE -ne 0" in updater


def test_update_writes_version_only_after_install_validation():
    updater = read_project_file("scripts/auto-update.ps1")

    assert "Copy-Item -Recurse -Force $installDir $programBackupDir" in updater
    assert updater.index("Set-Content -Path $VerFile") > updater.index("Test-Path ($installDir + '\\xiaoda-agent.exe')")


def test_frozen_config_writes_use_resolved_writable_directory():
    config_source = read_project_file("config.py")

    # CONFIG_DIR 统一使用 _resolve_data_path，确保 KIOXIA 只读时回退一致
    assert 'CONFIG_DIR = _resolve_data_path(_KIOXIA_BASE / "config"' in config_source
    # AGENT_CONFIG_PATH 和 AGENTS_CONFIG_DIR 都从 CONFIG_DIR 派生，
    # 不再各自独立判断路径（Qodo 审查：避免读写路径不一致）
    assert "AGENT_CONFIG_PATH = CONFIG_DIR / " in config_source
    assert "AGENTS_CONFIG_DIR = CONFIG_DIR / " in config_source


def test_manual_release_version_is_checked_against_source():
    workflow = read_project_file(".github/workflows/build-release.yml")

    assert "workflow_dispatch version must match pyproject.toml" in workflow
