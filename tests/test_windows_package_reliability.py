import ast
import json
import os
import re
import subprocess
import sys
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


# ── wheel py-modules 契约 ────────────────────────────────────
# 原缺陷：console script 指 agent:main，但 packages.find 只收「包」，
# 根目录单文件模块不进 wheel——pip install 后入口 ModuleNotFoundError（已实证）。


def declared_py_modules() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["tool"]["setuptools"]["py-modules"]


IMPORTED_ROOT_MODULES = {
    "agent", "agent_context", "agent_dispatcher", "belief_router",
    "botpy_compat", "channel_adapter_base", "cli", "cli_client",
    "cli_menu", "cli_palette", "config", "config_agents",
    "config_constants", "config_paths", "config_providers", "hooks",
    "ilink_client", "instinct_manager", "model_router",
    "model_router_config", "model_router_registry", "prompt_builder",
    "qq_bot_adapter", "setup_wizard", "slash_commands",
    "wechat_bot_adapter", "xiaoli_agent",
}


def test_console_script_entry_module_is_declared():
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]
    entry = project["scripts"]["xiaoda-agent"]
    module = entry.split(":")[0]
    assert module in declared_py_modules(), (
        f"console script 指向 {entry}，但 {module} 未声明进 py-modules"
    )


def test_py_modules_cover_every_imported_root_module():
    """AST 扫描源码树（排除 tests/chaos/vendor 等非分发目录），任何被 import
    的根目录单文件模块都必须声明在 py-modules，漏列即红。"""
    skip_dirs = {"tests", "chaos", "build", "dist", "__pycache__", "node_modules",
                 "vendor", "rust_core", ".git", "xiaoda_agent.egg-info", "evaluation",
                 "web", "docs", "assets", "logs", "data", "state", "files", "market",
                 "media", "models", "plugins_data", "quality", "specs", "credentials",
                 "deploy", "doctor", "instinct_manager_data"}
    imported: set[str] = set()
    candidates = set(IMPORTED_ROOT_MODULES)
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            dirnames[:] = []
            continue
        # 跳过隐藏目录（.venv/.github 等）与无 .py 的目录树
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            try:
                tree = ast.parse((Path(dirpath) / fn).read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        candidates.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    candidates.add(node.module.split(".")[0])
    # 只关心「仓库根目录确实存在对应 .py 文件」的模块
    existing = {m for m in candidates if (ROOT / f"{m}.py").is_file()}
    declared = set(declared_py_modules())
    missing = sorted(existing - declared)
    assert not missing, f"以下被 import 的根目录模块未进 py-modules（wheel 安装后必炸）: {missing}"


def test_wheel_smoke_job_gates_entrypoint():
    workflow = read_project_file(".github/workflows/build-release.yml")
    smoke_section = workflow[workflow.index("  wheel-smoke:"):]
    assert "python -m build --wheel" in smoke_section
    assert "clean-venv/bin/xiaoda-agent --help" in smoke_section


def test_config_data_dir_is_not_discovered_as_package():
    """config/ 目录是数据目录不是 Python 包；packages.find 必须显式排除，
    否则其 json5/yaml 会以 namespace 包语义装进 site-packages/config/，
    遮蔽源码树根的 config.py。"""
    with (ROOT / "pyproject.toml").open("rb") as file:
        find_cfg = tomllib.load(file)["tool"]["setuptools"]["packages"]["find"]
    excludes = find_cfg.get("exclude") or []
    assert any(e == "config" or e.startswith("config.") for e in excludes), (
        "packages.find 缺少 config 排除：数据目录会被当 namespace 包安装并遮蔽 config.py"
    )


# ── 安全扫描门禁 ─────────────────────────────────────────────
# 原缺陷：bandit/pip-audit 带 || true——扫描失败被吞，security job 永远假绿。


def test_security_gate_script_exists_and_is_fail_closed():
    gate = ROOT / "scripts" / "security_gate.py"
    assert gate.is_file()
    source = gate.read_text(encoding="utf-8")
    # 报告缺失/非法必须失败（不允许工具静默失效）
    assert "fail-closed" in source or "fail_closed" in source.lower()


def test_security_baseline_declares_zero_new_high_allowance():
    baseline_path = ROOT / "scripts" / "security_baseline.json"
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    # 门禁语义：high_count 是允许的新增未豁免 HIGH 数量；存量 HIGH 必须逐条豁免并给理由
    assert data["high_count"] == 0, (
        "baseline.high_count 应为 0（存量 HIGH 已逐条豁免）；放宽需在 PR 说明理由"
    )
    for exemption in data.get("exemptions", []):
        assert exemption.get("reason"), f"豁免条目缺 reason: {exemption}"


def test_ci_security_job_has_no_fail_open_scans():
    workflow = read_project_file(".github/workflows/ci-tests.yml")
    security_section = workflow[workflow.index("  security:"):]
    # 旧 fail-open 写法不得回归
    assert "-f screen || true" not in security_section
    assert "pip-audit --desc || true" not in security_section
    # 扫描必须经门禁脚本判定
    assert "python scripts/security_gate.py bandit-report.json" in security_section
    assert "python scripts/security_gate.py pip-audit-report.json" in security_section


def test_security_gate_blocks_new_high_and_passes_exemption(tmp_path):
    """端到端：基线报告（含豁免）通过；注入新 HIGH 即红。"""
    gate_script = ROOT / "scripts" / "security_gate.py"
    report = {
        "results": [
            {"filename": "./evaluation/retrieval_pipeline_harness.py", "line_number": 132,
             "test_id": "B324", "issue_severity": "HIGH", "issue_text": "known exempted"},
        ]
    }
    path = tmp_path / "bandit-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    passed = subprocess.run(
        [sys.executable, str(gate_script), str(path)],
        capture_output=True, text=True,
    )
    assert passed.returncode == 0

    report["results"].append(
        {"filename": "./new_regression.py", "line_number": 1,
         "test_id": "B101", "issue_severity": "HIGH", "issue_text": "new"}
    )
    path.write_text(json.dumps(report), encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, str(gate_script), str(path)],
        capture_output=True, text=True,
    )
    assert failed.returncode == 1


def test_security_gate_fails_on_missing_report(tmp_path):
    gate_script = ROOT / "scripts" / "security_gate.py"
    result = subprocess.run(
        [sys.executable, str(gate_script), str(tmp_path / "nope.json")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


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


def test_windows_build_installs_conpty_extra_and_verifies_bundle():
    """Windows 构建必须装 .[windows] 并断言 pywinpty 进 dist。

    原缺陷：ConPTY 依赖是懒加载可选依赖，PyInstaller 静态分析不收集，
    构建又没装也没验——Windows 终端静默降级为无 TTY 管道。"""
    workflow = read_project_file(".github/workflows/build-release.yml")
    build_section = workflow[workflow.index("  build:"):workflow.index("  wheel-smoke:")]
    # Windows job 安装 windows extra
    assert 'pip install ".[windows]"' in build_section
    # 打包校验步骤断言 winpty 产物进 dist
    assert "winpty*.pyd" in build_section or "*winpty*.dll" in build_section
    assert "winpty-agent.exe" in build_section
    # 校验步骤必须限定在 Windows runner
    verify_idx = build_section.index("Verify winpty ConPTY bundled")
    step_block = build_section[verify_idx - 200:]
    assert "runner.os == 'Windows'" in step_block

    # pyproject 必须声明 windows extra 且含 pywinpty
    with (ROOT / "pyproject.toml").open("rb") as file:
        extras = tomllib.load(file)["project"]["optional-dependencies"]
    win_deps = " ".join(extras["windows"])
    assert "pywinpty" in win_deps
    assert "pywin32" in win_deps


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
    # config.py Phase 1 拆分：路径常量已抽到 config_paths.py（config 同名 re-export）
    config_source = read_project_file("config_paths.py")

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


def test_linux_install_service_runs_as_non_root_user():
    """systemd unit 必须写死 User=/Group=：WebUI 带 pty.fork 终端，root 运行等于交出整台机器。

    2026-08-24 安全修复：
    - 安装时解析真实非 root 用户（SUDO_USER / logname / 当前用户）写入 unit
    - 纯 root 环境必须显式 --force 才继续安装服务
    - HOME 显式指向该用户 home，数据目录随用户 home 而非 root 的 $HOME
    """
    installer = read_project_file("scripts/install-linux.sh")
    # unit 写死运行用户与组
    assert "User=$SERVICE_USER" in installer, "unit 缺少 User= 指令"
    assert "Group=$SERVICE_GROUP" in installer, "unit 缺少 Group= 指令"
    # HOME 显式指向服务用户 home
    assert "Environment=HOME=$SERVICE_HOME" in installer, "unit 应显式设置 HOME"
    # 用户解析链：SUDO_USER 优先，logname 兜底
    assert "SUDO_USER" in installer, "应优先从 SUDO_USER 还原真实用户"
    assert "logname" in installer, "应以 logname 兜底还原真实用户"
    # root 执行且无真实用户时必须显式 --force
    assert '--force' in installer and 'FORCE=1' in installer, \
        "root 安装必须提供显式 --force 确认"
    # 数据目录以服务用户 home 为准（而非安装时 shell 的 $HOME）
    assert '$SERVICE_HOME/.ai-agent' in installer, "数据目录应基于服务用户 home"


def test_linux_install_service_hardening_directives():
    """systemd unit 应包含沙箱加固字段（2026-08-24 安全修复）。"""
    installer = read_project_file("scripts/install-linux.sh")
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=full",
        "PrivateTmp=true",
        "ProtectHome=read-only",
    ):
        assert directive in installer, f"unit 缺少加固指令: {directive}"
    # 对数据目录的可写白名单（其余路径在 ProtectHome=read-only 下只读）
    assert "ReadWritePaths=$DATA_DIR" in installer, \
        "unit 应为 ~/.ai-agent 数据目录声明 ReadWritePaths"


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


# ── updater 平台名契约 ────────────────────────────────────────
# 原缺陷：auto-update.sh 把 uname -m 的 aarch64 直接拼进资产名
# （linux-aarch64），而发布矩阵产物是 linux-arm64——ARM 用户永远匹配不到
# 资产、永远收不到更新。修复后 uname 输出经 resolve_release_platform 统一映射。


def test_linux_updater_maps_uname_arch_to_release_asset_names():
    updater = read_project_file("scripts/auto-update.sh")
    # 提取 resolve_release_platform 函数体，在受控环境执行并断言映射结果
    lines = updater.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("resolve_release_platform()"))
    end = next(i for i, ln in enumerate(lines[start:], start) if ln == "}")
    function_body = "\n".join(lines[start:end + 1])

    script = (
        "set -euo pipefail\n"
        + function_body
        + "\n"
        + 'case "$(resolve_release_platform "$1" "$2")" in\n'
    )

    def platform_for(os_name: str, arch: str, tmp_path: Path) -> str:
        runner = tmp_path / "resolve_platform.sh"
        runner.write_text(script + f'"{os_name}-*") ;;\n', encoding="utf-8")
        return ""

    # 用 bash 直接求值（避免拼 case 语法，简单调用即可）
    results = {}
    for os_name, arch in [("Linux", "x86_64"), ("Linux", "aarch64")]:
        proc = subprocess.run(
            ["bash", "-c", f"{function_body}\nresolve_release_platform {os_name} {arch}"],
            capture_output=True,
            text=True,
            check=True,
        )
        results[f"{os_name}-{arch}"] = proc.stdout.strip()

    # 发布矩阵（build-release.yml）产物命名：这两个平台必须有精确资产可下载
    assert results["Linux-x86_64"] == "linux-x86_64"
    assert results["Linux-aarch64"] == "linux-arm64"
    # 不支持平台必须返回空（触发跳过而非错误资产名）
    proc = subprocess.run(
        ["bash", "-c", f"{function_body}\nresolve_release_platform Darwin arm64"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ""


def test_updater_asset_patterns_cover_every_release_matrix_platform(tmp_path):
    """模拟 release JSON：每个构建矩阵平台都能命中资产名（updater 契约）。"""
    workflow = read_project_file(".github/workflows/build-release.yml")

    # 从 build-release.yml 矩阵中提取全部 platform 值
    matrix_platforms = re.findall(r"platform:\s*(\S+)", workflow)
    assert set(matrix_platforms) == {"windows-x64", "linux-x86_64", "linux-arm64"}
    version = "9.9.9"

    # 模拟 GitHub Release JSON：为每个矩阵平台生成真实命名的 tar.gz 资产
    assets = {
        f"xiaoda-agent-{p}-v{version}.tar.gz": f"https://example.com/xiaoda-agent-{p}-v{version}.tar.gz"
        for p in matrix_platforms
    }
    release_json = json.dumps({
        "tag_name": f"v{version}",
        "assets": [
            {"name": name, "browser_download_url": url}
            for name, url in assets.items()
        ],
    })

    for platform in matrix_platforms:
        pattern = f"xiaoda-agent-{platform}-v{version}.tar.gz"

        if platform == "windows-x64":
            # Windows updater: '*windows-x64*.tar.gz' 通配
            matched = [n for n in assets if "windows-x64" in n and n.endswith(".tar.gz")]
            assert matched, f"Windows updater pattern misses asset for {platform}"
        else:
            # Linux updater: PATTERN="xiaoda-agent-${PLATFORM}-v${LATEST_VERSION}.${EXT}"
            # 再现其两段匹配逻辑：精确 pattern → 模糊 ${PLATFORM}
            exact = [n for n in assets if n == pattern]
            fuzzy = [n for n in assets if platform in n]
            assert exact or fuzzy, f"Linux updater cannot match any asset for {platform}"
            assert exact, f"exact pattern should match: {pattern}"

        # release JSON 中确实存在该平台资产（防「匹配逻辑对但资产缺失」）
        assert pattern in assets

    # aarch64 主机经 resolve_release_platform 映射后命中 linux-arm64 资产
    updater = read_project_file("scripts/auto-update.sh")
    lines = updater.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("resolve_release_platform()"))
    end = next(i for i, ln in enumerate(lines[start:], start) if ln == "}")
    function_body = "\n".join(lines[start:end + 1])
    mapped = subprocess.run(
        ["bash", "-c", f"{function_body}\nresolve_release_platform Linux aarch64"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert f"xiaoda-agent-{mapped}-v{version}.tar.gz" in assets


def test_linux_updater_is_fail_closed_on_missing_checksum():
    """SHA256 校验文件缺失/非法必须中止安装，不允许 fail-open。"""
    updater = read_project_file("scripts/auto-update.sh")
    # 旧 fail-open 文案不得回归
    assert "跳过校验" not in updater
    assert "Warning: 未找到 SHA256" not in updater
    # fail-closed 分支存在且中止
    assert "fail-closed" in updater
    missing_idx = updater.index("未找到 SHA256 校验文件")
    # 缺失分支后必须置失败标志并中止
    after_missing = updater[missing_idx:]
    assert "VERIFY_FAILED=1" in after_missing[:400]
    # 校验文件格式校验（64 位十六进制）
    assert "[0-9a-f]{64}" in updater or "64}" in updater


def test_windows_updater_is_fail_closed_on_missing_checksum():
    """PowerShell updater 同样 fail-closed：缺 .sha256 即 exit 1。"""
    updater = read_project_file("scripts/auto-update.ps1")
    # 旧 fail-open 文案不得回归
    assert "skipping verification" not in updater
    assert "not found, skipping" not in updater
    assert "(fail-closed)" in updater
    # 缺失校验文件的分支必须 exit 1
    missing_idx = updater.index("SHA256 checksum file not available")
    assert "exit 1" in updater[missing_idx:missing_idx + 300]
    # 格式校验
    assert "^[0-9a-f]{64}$" in updater


def test_local_run_package_starts_with_installer_shebang():
    release_script = read_project_file("scripts/build-release.sh")
    marker_idx = release_script.index("__ARCHIVE__")
    installer_idx = release_script.index('cat "$SCRIPT_DIR/install-linux.sh"')
    assert installer_idx < marker_idx


# ── .run 自解压 marker 锚定契约 ──────────────────────────────
# 原缺陷：install-linux.sh 用非锚定 grep '__ARCHIVE__' | head -1 定位 payload
# 起点，脚本头部注释里的 __ARCHIVE__ 字样被误当 marker，tail 输出纯文本，
# tar 解压必坏。修复后：只匹配整行 ^__ARCHIVE__$ 且取最后一次命中。


def installer_marker_extraction_lines() -> list:
    installer = read_project_file("scripts/install-linux.sh").splitlines()
    return [
        line.strip()
        for line in installer
        if "grep" in line and "__ARCHIVE__" in line and "grep -q" not in line
    ]


def test_linux_run_extraction_anchors_archive_marker_to_full_line():
    locator = next(
        line
        for line in installer_marker_extraction_lines()
        if "archive_line=" in line
    )
    # 必须锚定整行（^__ARCHIVE__$），防止注释/字符串中的字样被误判
    assert "'^__ARCHIVE__$'" in locator or '"^__ARCHIVE__$"' in locator


def test_linux_run_extraction_takes_last_marker_occurrence():
    locator = next(
        line
        for line in installer_marker_extraction_lines()
        if "archive_line=" in line
    )
    # 打包端在脚本之后追加 marker + payload，真实 marker 是最后一个锚定命中
    assert "tail -1" in locator
    assert "head -1" not in locator


def test_linux_run_extraction_rejects_unanchored_first_match():
    """旧缺陷写法（非锚定 + head -1）不得回归"""
    updater = read_project_file("scripts/install-linux.sh")
    assert "grep -n '__ARCHIVE__' \"$0\" | head -1" not in updater
    assert 'grep -n \'__ARCHIVE__\' "$0"' not in updater


def test_ci_smoke_tests_run_self_extracting_installer():
    workflow = read_project_file(".github/workflows/build-release.yml")
    assert "Smoke test .run self-extracting installer" in workflow
    # smoke 必须验证 payload 以 gzip magic 开头，且对 decoy 注释免疫
    assert "1f8b" in workflow
    assert "^__ARCHIVE__$" in workflow
    assert "decoy comment mentioning __ARCHIVE__" in workflow


def test_release_waits_for_test_job():
    workflow = read_project_file(".github/workflows/build-release.yml")
    test_section = workflow[workflow.index("  test:"):workflow.index("  release:")]
    # 2026-08-23 门禁收紧：全集测试步骤不再 continue-on-error——任何失败
    # 直接使 test job 失败，release 不触发（critical strict 子集保留作二道闸）。
    assert "continue-on-error: true" not in test_section
    assert "Run critical tests (strict)" in test_section
    # release 仍显式依赖 test job（job 内 strict 步骤失败 ⇒ test job 失败 ⇒ release 不触发）
    assert "needs: [build, test]" in workflow


def test_linux_operational_scripts_use_installed_service_name():
    paths = [
        "scripts/start.sh",
        "scripts/healthcheck.sh",
        "scripts/block_watchdog.sh",
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
