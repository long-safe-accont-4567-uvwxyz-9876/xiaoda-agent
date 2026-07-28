# Windows Package Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复除 Gitee 上传门禁外的全部已确认 Windows 安装包可靠性问题，并让 CI、本地发布、安装、启动、升级、卸载使用一致的文件与版本规则。

**Architecture:** 以 `start-windows.bat` 为唯一交互启动入口；以更新候选目录校验、安装目录备份和最后写入版本号作为自动更新的原子协议；以同一 Windows 辅助脚本清单消除 CI、PyInstaller 与本地发布的差异。配置目录写入统一采用现有 `_resolve_data_path` 的可写性回退路径。

**Tech Stack:** Python 3.11、pytest、PyInstaller、NSIS、Windows cmd、PowerShell、GitHub Actions。

## Global Constraints

- 不修改 Gitee Release 上传失败门禁与校验逻辑。
- 不在安装包、日志或测试输出中暴露令牌、密钥或个人凭证。
- 保持 per-user 安装与无需管理员权限的行为。
- 自动更新必须仅在用户显式创建 `.auto_update` 后启用。
- 自动更新失败时不得写入新版本号，不得留下半更新的程序目录。

---

### Task 1: 建立安装包回归测试

**Files:**
- Create: `tests/test_windows_package_reliability.py`
- Test: `tests/test_windows_package_reliability.py`

**Interfaces:**
- Consumes: `scripts/installer.nsi`、`scripts/start-windows.bat`、`scripts/auto-update.ps1`、`scripts/build-release.sh`、`xiaoda-agent.spec`、`.github/workflows/build-release.yml`。
- Produces: 对脚本清单、快捷方式、自动更新和版本门禁的静态回归保护。

- [ ] **Step 1: 写失败测试**

```python
def test_windows_package_uses_launcher_for_shortcuts():
    assert 'CreateShortCut "$DESKTOP\\小妲Agent.lnk" "$INSTDIR\\start-windows.bat" "--desktop"' in installer

def test_all_windows_packaging_paths_include_update_ps1():
    assert 'auto-update.ps1' in spec
    assert 'auto-update.ps1' in local_release
    assert 'auto-update.ps1' in workflow

def test_ci_does_not_enable_auto_update_by_default():
    assert 'New-Item -Path "dist/xiaoda-agent/.auto_update"' not in workflow
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 失败，指出快捷方式、`.auto_update` 或本地脚本清单不符合要求。

- [ ] **Step 3: 实现后续任务中的最小代码改动**

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add tests/test_windows_package_reliability.py
git commit -m "test: cover Windows package reliability"
```

### Task 2: 统一安装启动、卸载与本地辅助脚本清单

**Files:**
- Modify: `scripts/installer.nsi:107-110,129,176-180`
- Modify: `scripts/build-release.sh:157-161`
- Modify: `xiaoda-agent.spec:79-83`
- Modify: `scripts/doctor.bat:53-60`

**Interfaces:**
- Consumes: 安装目录 `$INSTDIR`、`start-windows.bat --desktop`。
- Produces: 一致的安装启动入口、可卸载路径和完整的本地构建辅助脚本。

- [ ] **Step 1: 扩展失败测试**

```python
def test_uninstall_command_is_quoted():
    assert '"$INSTDIR\\uninstall.exe"' in uninstall_registry_value

def test_doctor_uses_delayed_errorlevel_for_py_launcher():
    assert 'if !errorlevel! equ 0' in doctor
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 失败，指出未加引号的卸载命令和 `%errorlevel%`。

- [ ] **Step 3: 实现最小修复**

```nsi
CreateShortCut "$DESKTOP\小妲Agent.lnk" "$INSTDIR\start-windows.bat" "--desktop" "$INSTDIR\xiaoda-icon.ico" 0
WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}" "UninstallString" '"$INSTDIR\uninstall.exe"'
```

将 `auto-update.ps1` 加入本地发布脚本与 PyInstaller 脚本清单；将 `doctor.bat` 的两个块内错误码判断改为 `!errorlevel!`；PATH 删除增加“PATH 恰等于 `$INSTDIR`”的处理。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add scripts/installer.nsi scripts/build-release.sh xiaoda-agent.spec scripts/doctor.bat tests/test_windows_package_reliability.py
git commit -m "fix(windows): unify package launch and support files"
```

### Task 3: 让自动更新成为可回滚的原子操作

**Files:**
- Modify: `scripts/auto-update.ps1:1-136`
- Test: `tests/test_windows_package_reliability.py`

**Interfaces:**
- Consumes: `InstallDir`、下载的 `.tar.gz`、`.version`。
- Produces: 只有完整候选包通过校验且复制完成时才更新安装目录与 `.version`。

- [ ] **Step 1: 写失败测试**

```python
def test_update_checks_tar_exit_code_before_installing():
    assert '$LASTEXITCODE -ne 0' in updater

def test_update_writes_version_only_after_install_validation():
    assert updater.index('Set-Content -Path $VerFile') > updater.index('Test-Path ($installDir + \'\\xiaoda-agent.exe\')')
    assert 'Copy-Item -Recurse -Force $installDir $programBackupDir' in updater
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 失败，指出 `tar` 退出码、程序目录备份或版本写入顺序缺失。

- [ ] **Step 3: 实现最小修复**

在 PowerShell 脚本开始处启用 `$ErrorActionPreference = 'Stop'`；对 `tar` 明确检查 `$LASTEXITCODE`；解压后验证候选目录含 `xiaoda-agent.exe`、`start-windows.bat`、`auto-update.bat`、`auto-update.ps1`、`doctor.bat`；停止旧进程后备份整个安装目录；复制失败或关键文件校验失败时恢复该程序目录；仅在所有校验成功后写 `.version`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add scripts/auto-update.ps1 tests/test_windows_package_reliability.py
git commit -m "fix(update): make Windows updates atomic"
```

### Task 4: 修复 frozen 目录初始化与默认更新开关

**Files:**
- Modify: `config.py:207-227,375-378`
- Modify: `.github/workflows/build-release.yml:362-364,336-345,554-562`
- Test: `tests/test_windows_package_reliability.py`

**Interfaces:**
- Consumes: `_resolve_data_path(kioxia_path, fallback_path)`。
- Produces: 所有 frozen 写目录可在 KIOXIA 路径不可写时回退；CI 不默认启用自动更新；手动发布版本必须匹配源码版本。

- [ ] **Step 1: 写失败测试**

```python
def test_frozen_config_writes_use_resolved_writable_directory():
    assert '_resolve_data_path(_KIOXIA_BASE / "config"' in config_source
    assert 'AGENTS_CONFIG_DIR = _resolve_data_path(' in config_source

def test_manual_release_version_is_checked_against_source():
    assert 'workflow_dispatch version must match pyproject.toml' in workflow
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 失败，指出 frozen 配置直写、默认自动更新或 dispatch 版本门禁缺失。

- [ ] **Step 3: 实现最小修复**

让 `_init_user_resources` 从 `_resolve_data_path(_KIOXIA_BASE / "config", _FALLBACK_BASE / "config")` 得到目标目录；让 `AGENTS_CONFIG_DIR` 使用同一函数；从 CI 删除 `.auto_update` 创建；在版本检查步骤中读取源码版本并拒绝不匹配的 dispatch 输入。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_windows_package_reliability.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add config.py .github/workflows/build-release.yml tests/test_windows_package_reliability.py
git commit -m "fix(package): harden frozen paths and release versioning"
```

### Task 5: 全量静态与构建验证

**Files:**
- Modify: `VERSION`, `pyproject.toml`, `.version`, `web/frontend/package.json`

**Interfaces:**
- Consumes: `scripts/check_version_sync.py`。
- Produces: 新版本的同步源码与可构建发行包。

- [ ] **Step 1: 升级版本并同步四个版本源**

Run: `printf '0.5.45\n' > VERSION && python scripts/check_version_sync.py --fix && python scripts/check_version_sync.py --ci`

Expected: `all versions in sync (0.5.45)`。

- [ ] **Step 2: 验证 Python 与 PowerShell 语法**

Run: `python -m py_compile config.py && python -m pytest tests/test_windows_package_reliability.py -q`

Run: `pwsh -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/auto-update.ps1 -Raw)) | Out-Null"`

Expected: 三项均成功。

- [ ] **Step 3: 验证辅助脚本清单一致性**

Run: `python -c "from pathlib import Path; names=('start-windows.bat','auto-update.bat','auto-update.ps1','open-browser.ps1','doctor.bat'); assert all((Path('scripts') / n).is_file() for n in names)"`

Expected: 退出码 0。

- [ ] **Step 4: 提交**

```bash
git add VERSION pyproject.toml .version web/frontend/package.json
git commit -m "chore(release): bump version to 0.5.45"
```
