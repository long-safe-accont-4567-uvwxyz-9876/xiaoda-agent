import ast
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def read_project_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def gitee_release_script() -> str:
    workflow = yaml.safe_load(read_project_file(".github/workflows/build-release.yml"))
    steps = workflow["jobs"]["release"]["steps"]
    return next(step["run"] for step in steps if step["name"].startswith("Sync release to Gitee"))


def run_gitee_release_script(tmp_path: Path, curl_body: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(curl_body, encoding="utf-8")
    curl.chmod(0o755)
    env = os.environ | {
        "GITEE_TOKEN": "test-token",
        "VERSION": "1.2.3",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", "-c", gitee_release_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def create_required_gitee_artifacts(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    names = (
        "xiaoda-agent-windows-x64-v1.2.3-setup.exe",
        "xiaoda-agent-windows-x64-v1.2.3.tar.gz",
        "xiaoda-agent-linux-x86_64-v1.2.3.tar.gz",
        "xiaoda-agent-linux-x86_64-v1.2.3-install.sh",
        "xiaoda-agent-linux-x86_64-v1.2.3.run",
        "xiaoda-agent-linux-arm64-v1.2.3.tar.gz",
        "xiaoda-agent-linux-arm64-v1.2.3-install.sh",
        "xiaoda-agent-linux-arm64-v1.2.3.run",
    )
    for name in names:
        artifact = tmp_path / "artifacts" / name
        artifact.write_text("artifact", encoding="utf-8")
        (artifact.parent / f"{name}.sha256").write_text("checksum", encoding="utf-8")


def test_local_ai_dependency_marker_matches_supported_release_platforms():
    with (ROOT / "pyproject.toml").open("rb") as file:
        local_ai = tomllib.load(file)["project"]["optional-dependencies"]["local-ai"]
    requirement = Requirement(local_ai[0])
    assert requirement.name == "onnxruntime-genai"
    assert str(requirement.specifier) == "==0.15.2"
    assert requirement.marker is not None
    assert requirement.marker.evaluate(
        {"platform_system": "Linux", "platform_machine": "aarch64"}
    )
    assert requirement.marker.evaluate(
        {"platform_system": "Linux", "platform_machine": "x86_64"}
    )
    assert requirement.marker.evaluate(
        {"platform_system": "Windows", "platform_machine": "AMD64"}
    )
    assert not requirement.marker.evaluate(
        {"platform_system": "Darwin", "platform_machine": "arm64"}
    )
    assert not requirement.marker.evaluate(
        {"platform_system": "Windows", "platform_machine": "ARM64"}
    )


def test_pyinstaller_collects_local_ai_and_gateway_platform_adapters():
    spec = read_project_file("xiaoda-agent.spec")
    assert "collect_submodules('local_ai')" in spec
    assert "collect_submodules('llm_gateway')" in spec


def test_docker_build_context_excludes_partial_downloads_globally():
    patterns = [
        line.strip()
        for line in read_project_file(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "*.part" in patterns


def test_pyinstaller_tree_datas_excludes_partial_downloads_by_behavior(tmp_path):
    module = ast.parse(read_project_file("xiaoda-agent.spec"))
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_tree_datas"
    )
    source_root = tmp_path / "config"
    source_root.mkdir()
    (source_root / "ready.bin").write_text("ready", encoding="utf-8")
    (source_root / "downloading.bin.part").write_text("partial", encoding="utf-8")
    namespace = {"os": os, "SPECPATH": str(tmp_path)}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "xiaoda-agent.spec", "exec"),
        namespace,
    )

    bundled = namespace["_tree_datas"](str(source_root), "config")

    assert bundled == [(str(source_root / "ready.bin"), "config")]


def test_python_distribution_includes_local_ai_and_gateway_packages():
    with (ROOT / "pyproject.toml").open("rb") as file:
        packages = tomllib.load(file)["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "local_ai*" in packages
    assert "llm_gateway*" in packages


def test_pyinstaller_dynamic_library_collection_isolated_per_package():
    import ast

    module = ast.parse(read_project_file("xiaoda-agent.spec"))
    loop = next(
        node
        for node in module.body
        if isinstance(node, ast.For)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "collect_dynamic_libs"
            for child in ast.walk(node)
        )
    )
    attempted = []

    def collect_dynamic_libs(package: str):
        attempted.append(package)
        if package == "sqlite_vec":
            raise RuntimeError("missing native package")
        return [(package, ".")]

    namespace = {"binaries": [], "collect_dynamic_libs": collect_dynamic_libs}
    exec(compile(ast.Module(body=[loop], type_ignores=[]), "xiaoda-agent.spec", "exec"), namespace)
    assert attempted == [
        "pilk",
        "sqlite_vec",
        "onnxruntime",
        "onnxruntime_genai",
        "tokenizers",
    ]
    assert namespace["binaries"] == [
        ("pilk", "."),
        ("onnxruntime", "."),
        ("onnxruntime_genai", "."),
        ("tokenizers", "."),
    ]


def test_supported_release_jobs_install_local_ai_dependencies():
    workflow = read_project_file(".github/workflows/build-release.yml")
    build_section = workflow[workflow.index("  build:"):workflow.index("  test:")]
    assert "windows-x64" in build_section
    assert "linux-x86_64" in build_section
    assert "linux-arm64" in build_section
    assert "ubuntu-24.04-arm" in build_section
    assert 'pip install ".[local-ai]"' in build_section


def test_release_validates_ort_genai_in_frozen_executable_and_native_bundle():
    workflow = read_project_file(".github/workflows/build-release.yml")
    build_section = workflow[workflow.index("  build:"):workflow.index("  test:")]
    assert "pyi-archive_viewer" in build_section
    assert "onnxruntime_genai" in build_section
    assert "onnxruntime-genai*.dll" in build_section
    assert "libonnxruntime-genai*.so*" in build_section
    assert "local-ai-smoke" in build_section
    assert '"$EXE" local-ai-smoke "$SMOKE_DIR"' in build_section


def test_release_artifacts_include_linux_arm64_frozen_bundle():
    workflow = read_project_file(".github/workflows/build-release.yml")
    assert "xiaoda-agent-linux-arm64-*.tar.gz" in workflow
    assert "xiaoda-agent-linux-arm64-*.run" in workflow


def test_gitee_release_sync_uploads_all_linux_arm64_artifacts():
    workflow = read_project_file(".github/workflows/build-release.yml")
    gitee_section = workflow[workflow.index("Sync release to Gitee"):]
    assert "xiaoda-agent-linux-arm64-*.tar.gz" in gitee_section
    assert "xiaoda-agent-linux-arm64-*-install.sh" in gitee_section
    assert "xiaoda-agent-linux-arm64-*.run" in gitee_section


def test_gitee_release_sync_fails_when_required_artifact_is_missing(tmp_path):
    create_required_gitee_artifacts(tmp_path)
    (tmp_path / "artifacts" / "xiaoda-agent-linux-arm64-v1.2.3.run").unlink()

    result = run_gitee_release_script(
        tmp_path,
        "#!/usr/bin/env bash\nprintf '{\"id\": 42}'\n",
    )

    assert result.returncode != 0


@pytest.mark.parametrize("failed_operation", ["create", "upload"])
def test_gitee_release_sync_fails_on_http_error(tmp_path, failed_operation):
    create_required_gitee_artifacts(tmp_path)
    curl_body = """#!/usr/bin/env bash
if [[ "$*" == *"attach_files"* ]]; then
  [[ "${FAILED_OPERATION}" == "upload" ]] && exit 22
  printf '{"id": 43}'
  exit 0
fi
[[ "${FAILED_OPERATION}" == "create" ]] && exit 22
printf '{"id": 42}'
"""
    bin_dir = tmp_path / "bin"
    artifacts = tmp_path / "artifacts"
    bin_dir.mkdir(exist_ok=True)
    artifacts.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(curl_body, encoding="utf-8")
    curl.chmod(0o755)
    env = os.environ | {
        "FAILED_OPERATION": failed_operation,
        "GITEE_TOKEN": "test-token",
        "VERSION": "1.2.3",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        ["bash", "-c", gitee_release_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_release_never_bundles_market_model_storage_or_partial_downloads():
    spec = read_project_file("xiaoda-agent.spec")
    assert "local_model_storage" not in spec
    assert "*.part" not in spec
    assert "LOCAL_AI_STORAGE_DIR" not in spec


def test_local_release_installs_and_verifies_local_ai_runtime():
    release_script = read_project_file("scripts/build-release.sh")
    assert 'python3 -m pip install ".[local-ai]"' in release_script
    assert "onnxruntime_genai" in release_script
    assert "local-ai-smoke" in release_script
    assert "onnxruntime-genai*.dll" in release_script
    assert "libonnxruntime-genai*.so*" in release_script
    assert "create_ort_genai_smoke_model.py" in release_script


def test_docker_image_installs_local_ai_runtime_without_model_storage():
    dockerfile = read_project_file("Dockerfile")
    assert 'RUN pip install --no-cache-dir --prefix=/install ".[local-ai]"' in dockerfile
    assert "import onnxruntime_genai" in dockerfile
    assert "local_model_storage" not in dockerfile
    assert "*.part" not in dockerfile


def test_ort_genai_smoke_model_generator_is_shared_by_ci_and_local_release():
    workflow = read_project_file(".github/workflows/build-release.yml")
    release_script = read_project_file("scripts/build-release.sh")
    generator = ROOT / "scripts/create_ort_genai_smoke_model.py"
    assert generator.is_file()
    assert "scripts/create_ort_genai_smoke_model.py" in workflow
    assert "scripts/create_ort_genai_smoke_model.py" in release_script


def test_windows_package_uses_exe_for_shortcuts():
    """新设计：快捷方式直接指向 xiaoda-agent.exe（内部已带看门狗，崩溃/卡死自动重启）。
    start-windows.bat 包装层已被 exe 一体化取代，不再打包。"""
    installer = read_project_file("scripts/installer.nsi")

    assert 'CreateShortCut "$DESKTOP\\小妲Agent.lnk" "$INSTDIR\\xiaoda-agent.exe"' in installer
    assert 'CreateShortCut "$SMPROGRAMS\\${PRODUCT_NAME}\\小妲Agent.lnk" "$INSTDIR\\xiaoda-agent.exe"' in installer
    # 旧包装层不应再被打包/引用
    assert "start-windows.bat" not in installer


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

    # CONFIG_DIR 固定系统盘用户目录（不随 KIOXIA_DATA_DIR 走）：
    # agent.json5/人格 MD 等配置文件避免 U 盘 IO 拖慢请求，只有数据库 DATA_DIR 保留 U 盘
    assert 'CONFIG_DIR = Path.home() / ".ai-agent" / "config"' in config_source
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


def test_linux_frozen_launcher_uses_bundled_executable():
    start_script = read_project_file("scripts/start-linux.sh")
    assert '${INSTALL_DIR}/xiaoda-agent' in start_script
    executable_idx = start_script.index('${INSTALL_DIR}/xiaoda-agent')
    source_fallback_idx = start_script.index('${INSTALL_DIR}/agent.py')
    assert executable_idx < source_fallback_idx


def test_linux_updater_accepts_frozen_bundle_contract():
    updater = read_project_file("scripts/auto-update.sh")
    critical_line = next(line for line in updater.splitlines() if line.startswith("CRITICAL_FILES="))
    assert "xiaoda-agent" in critical_line
    assert "agent.py" not in critical_line


def test_linux_updater_rejects_release_downgrades():
    updater = read_project_file("scripts/auto-update.sh")
    assert "sort -V" in updater
    assert 'LATEST_VERSION" != "$CURRENT_VERSION' in updater


def test_local_run_package_starts_with_installer_shebang():
    release_script = read_project_file("scripts/build-release.sh")
    marker_idx = release_script.index("__ARCHIVE__")
    installer_idx = release_script.index('cat "$SCRIPT_DIR/install-linux.sh"')
    assert installer_idx < marker_idx


def test_release_waits_for_test_job():
    workflow = read_project_file(".github/workflows/build-release.yml")
    assert "continue-on-error: true" not in workflow[workflow.index("  test:"):workflow.index("  release:")]
    assert "needs: [build, test]" in workflow


def test_linux_operational_scripts_use_installed_service_name():
    paths = [
        "scripts/start.sh",
        "scripts/healthcheck.sh",
        "scripts/block_watchdog.sh",
        "scripts/block_watchdog2.sh",
        "slash_commands.py",
    ]
    for path in paths:
        content = read_project_file(path)
        assert "nahida-web" not in content, path
        assert "xiaoda-agent" in content, path


def test_pyinstaller_bundles_all_builtin_lazy_tool_modules():
    spec = read_project_file("xiaoda-agent.spec")
    manifest = read_project_file("tools/_builtin_manifest.py")
    assert '"module_path": "tools.secrets_tool"' in manifest
    assert "'tools.secrets_tool'" in spec


def test_frozen_linux_installer_does_not_require_python_toolchain():
    installer = read_project_file("scripts/install-linux.sh")
    assert "pip3" not in installer
    assert "python3 -m venv" not in installer


def test_local_release_build_rebuilds_and_validates_frontend():
    release_script = read_project_file("scripts/build-release.sh")
    pyinstaller_idx = release_script.index("pyinstaller xiaoda-agent.spec")
    frontend_build_idx = release_script.index("npm run build")
    frontend_check_idx = release_script.index("web/dist/index.html")
    assert frontend_build_idx < pyinstaller_idx
    assert frontend_check_idx < pyinstaller_idx


def test_built_frontend_assets_are_not_gitignored():
    gitignore = read_project_file(".gitignore")
    assert "\nweb/dist/\n" not in gitignore
    assert "!web/dist/**" in gitignore


def test_ci_publishes_documented_linux_run_installer():
    workflow = read_project_file(".github/workflows/build-release.yml")
    assert "xiaoda-agent-linux-x86_64-v${VERSION}.run" in workflow
    release_files = workflow[workflow.index("      - name: Create GitHub Release"):]
    assert "artifacts/xiaoda-agent-linux-x86_64-*.run" in release_files


def test_ci_tag_version_must_match_source_version():
    workflow = read_project_file(".github/workflows/build-release.yml")
    assert workflow.count('tag version must match pyproject.toml') >= 3
