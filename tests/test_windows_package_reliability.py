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


# ── Linux 构建可靠性测试 ──────────────────────────────────────


def test_ci_linux_does_not_enable_auto_update_by_default():
    """Linux CI 不应默认创建 .auto_update（和 Windows 对齐）"""
    workflow = read_project_file(".github/workflows/build-release.yml")
    linux_section = workflow[workflow.index("Package (Linux x86_64)"):workflow.index("Upload artifact")]
    assert "touch dist/xiaoda-agent/.auto_update" not in linux_section


def test_ci_linux_packages_start_script():
    """Linux CI 打包应包含 start-linux.sh"""
    workflow = read_project_file(".github/workflows/build-release.yml")
    linux_section = workflow[workflow.index("Package (Linux x86_64)"):workflow.index("Upload artifact")]
    assert "scripts/start-linux.sh" in linux_section


def test_linux_updater_uses_flock_for_serialization():
    """Linux 自动更新应使用 flock 串行化覆盖完整更新事务（和 Windows mutex 对齐）"""
    updater = read_project_file("scripts/auto-update.sh")
    assert "flock" in updater
    # flock 必须在下载之前获取（覆盖完整事务）
    flock_idx = updater.index("flock -n 9")
    download_idx = updater.index("curl -Lf --progress-bar")
    assert flock_idx < download_idx


def test_linux_updater_checks_tar_exit_code():
    """Linux 自动更新应检查 tar 退出码"""
    updater = read_project_file("scripts/auto-update.sh")
    assert "tar xzf" in updater
    # tar 失败时应中止（set -euo pipefail + if ! tar ...）
    assert "if ! tar" in updater


def test_linux_updater_writes_version_only_after_validation():
    """Linux 自动更新：版本号在所有校验成功后才写入"""
    updater = read_project_file("scripts/auto-update.sh")
    # 版本号写入必须在候选包校验和复制之后
    version_write_idx = updater.index('echo "$LATEST_VERSION" > "$VERSION_FILE"')
    candidate_check_idx = updater.index("候选包校验通过")
    copy_idx = updater.index('cp -a "${CANDIDATE_DIR}/."')
    assert version_write_idx > candidate_check_idx
    assert version_write_idx > copy_idx


def test_linux_updater_has_backup_ready_flag():
    """Linux 自动更新：备份完整性标志（和 Windows $programBackupReady 对齐）"""
    updater = read_project_file("scripts/auto-update.sh")
    assert "BACKUP_READY=false" in updater
    assert "BACKUP_READY=true" in updater
    # 回滚时应检查 BACKUP_READY
    assert 'BACKUP_READY" = "true"' in updater


def test_linux_updater_validates_critical_files():
    """Linux 自动更新：校验候选包和安装后关键文件"""
    updater = read_project_file("scripts/auto-update.sh")
    assert "CRITICAL_FILES" in updater
    assert "MISSING_FILES" in updater


def test_linux_install_script_creates_all_data_dirs():
    """Linux 安装脚本：创建完整的数据目录（与 config.py 对齐）"""
    installer = read_project_file("scripts/install-linux.sh")
    required_dirs = [
        "db", "logs", "credentials", "config", "config/workspace",
        "config/agents", "stickers", "xiaoli-stickers", "agent-stickers",
        "media", "files", "voice_refs", "memory_state", "plugins", "workspace"
    ]
    for d in required_dirs:
        assert d in installer, f"install-linux.sh 缺少数据目录: {d}"


def test_linux_install_service_uses_start_script():
    """Linux systemd 服务应使用 start-linux.sh 而非直接调 python"""
    installer = read_project_file("scripts/install-linux.sh")
    assert "start-linux.sh" in installer
    assert "ExecStart=$INSTALL_DIR/scripts/start-linux.sh" in installer
    # 不应直接用 python agent.py 作为 ExecStart（绕过更新检查和看门狗）
    assert "ExecStart=$INSTALL_DIR/.venv/bin/python" not in installer
    # systemd 不支持 ${VAR:-default} 扩展，应使用 ${WEBUI_PORT}
    assert "WEBUI_PORT:-8082" not in installer
    # 看门狗达到上限后 exit 0，systemd 需配置 RestartPreventExitStatus
    assert "RestartPreventExitStatus" in installer


def test_dockerfile_injects_version():
    """Dockerfile 应从 pyproject.toml 注入 .version"""
    dockerfile = read_project_file("Dockerfile")
    assert "pyproject.toml" in dockerfile
    assert ".version" in dockerfile


def test_docker_build_supports_multi_arch():
    """Docker 构建应支持 amd64 + arm64，且 QEMU/Buildx 在 docker job 内"""
    workflow = read_project_file(".github/workflows/build-release.yml")
    docker_section = workflow[workflow.index("  docker:"):]
    assert "linux/amd64" in docker_section
    assert "linux/arm64" in docker_section
    assert "setup-qemu-action" in docker_section
    assert "setup-buildx-action" in docker_section
    # platforms 必须在 build-push-action 步骤中
    assert "platforms: linux/amd64,linux/arm64" in docker_section


def test_linux_watchdog_exits_zero_on_max_restarts():
    """看门狗达到 MAX_RESTARTS 后应 exit 0，否则 systemd 会继续重启"""
    start_script = read_project_file("scripts/start-linux.sh")
    # 定位到"达到 MAX_RESTARTS 后停止重启"的退出路径
    stop_idx = start_script.index("停止重启")
    exit_block = start_script[stop_idx:stop_idx + 300]
    assert "exit 0" in exit_block
    # 不应使用 exit $exit_code（非零退出码会导致 systemd 重启）
    assert "exit $exit_code" not in exit_block


def test_linux_updater_lock_path_is_install_specific():
    """auto-update.sh 锁路径应基于 INSTALL_DIR 哈希，不是固定 /tmp 路径"""
    updater = read_project_file("scripts/auto-update.sh")
    # 不应是固定路径（多个安装会互相阻塞）
    assert 'LOCK_FILE="/tmp/xiaoda-agent-update.lock"' not in updater
    # 应基于 INSTALL_DIR 生成唯一路径
    assert "md5sum" in updater or "INSTALL_DIR" in updater.split("LOCK_FILE=")[1].split("\n")[0]


def test_linux_updater_excludes_venv_from_backup():
    """auto-update.sh 备份应排除 .venv 以节省时间和空间"""
    updater = read_project_file("scripts/auto-update.sh")
    assert "exclude='.venv'" in updater or "--exclude" in updater
