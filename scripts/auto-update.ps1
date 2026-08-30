# ============================================
#   Xiaoda Agent - Auto-Update Script (PowerShell)
#   Checks GitHub Release for new versions
#   Called by auto-update.bat
#
#   原子更新协议（v0.5.45 重写）：
#     1. 下载 + SHA256 校验
#     2. 解压后校验候选目录包含全部关键文件
#     3. 备份用户数据 + 整个安装目录
#     4. 停止旧进程 → 清空安装目录（摘出 state 文件）→ 拷贝候选包 → 放回 state 文件
#     5. 校验安装后的关键文件齐全
#     6. 任何步骤失败 → 从备份恢复安装目录，不写新版本号
#     7. 全部成功 → 写 .version，清理临时文件
# ============================================

param(
    [string]$Repo = "long-safe-accont-4567-uvwxyz-9876/xiaoda-agent",
    [string]$CurrentVersion = "",
    [string]$FlagFile = "",
    [string]$VerFile = "",
    [string]$InstallDir = ""
)

# 严格模式：任何未捕获异常都进入 catch 块执行回滚
$ErrorActionPreference = "Stop"

# 更新改为手动触发（用户双击 auto-update.bat 或「检查更新」快捷方式）
# 不再检查 .auto_update 标志文件 —— 用户主动运行本脚本即视为同意更新
# $FlagFile 参数保留是为了向后兼容旧版 auto-update.bat 的传参，不再起门控作用

# CodeRabbit 审查：per-user named mutex 保证更新事务串行化
# 两个同时启动的更新共享临时/备份路径，会互相删工作文件导致回滚失效
$updateMutex = [System.Threading.Mutex]::new($false, 'Global\xiaoda-agent-update-mutex')
$updateMutexAcquired = $false
try {
    if (-not $updateMutex.WaitOne(0)) {
        Write-Host "  Another update is already running, skipping."
        exit 0
    }
    $updateMutexAcquired = $true
} catch {
    Write-Host "  Warning: could not acquire update mutex, proceeding without serialization."
}

# 关键文件清单：解压后和安装后都必须存在，缺一则判定更新失败
# 注：start-windows.bat 已移除——看门狗逻辑内置于 exe，快捷方式直接指向 xiaoda-agent.exe
$criticalFiles = @('xiaoda-agent.exe', 'auto-update.bat', 'auto-update.ps1', 'doctor.bat')

# 用户数据子目录清单：备份和恢复时使用
$userDataItems = @('.env', 'config', 'credentials', 'data', 'stickers', 'xiaoli-stickers', 'agent-stickers', 'media', 'voice_refs', 'files', 'memory_state', 'plugins')

# 程序目录备份：失败时用于完整回滚（区别于用户数据备份）
$programBackupDir = $null
$rolledBack = $false

try {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -TimeoutSec 10
    $latest = $release.tag_name -replace '^v', ''

    if ($latest -eq $CurrentVersion) {
        Write-Host "  Already up to date v$latest"
        exit 0
    }

    # Version comparison (root fix: prevent downgrade "updates" that cause crashes)
    # Original bug: only string equality check, v0.5.4 != v0.5.37 triggered download,
    # but v0.5.37 < v0.5.4 is a downgrade.
    # Use [Version] object comparison, only update when latest > current.
    try {
        $curVerObj = [Version]$CurrentVersion
        $latestVerObj = [Version]$latest
    } catch {
        Write-Host "  Version parse failed, skipping update"
        exit 0
    }

    if ($latestVerObj -le $curVerObj) {
        Write-Host "  Current v$CurrentVersion >= latest v$latest, no update needed"
        exit 0
    }

    Write-Host "  New version available: v$latest (current: v$CurrentVersion)"

    $asset = $release.assets | Where-Object { $_.name -like '*windows-x64*.tar.gz' } | Select-Object -First 1
    if (-not $asset) {
        Write-Host "  No Windows installer found, skipping"
        exit 0
    }

    # ── Step 1: 下载 ──
    Write-Host "  Downloading $($asset.name) ..."
    $tmp = [System.IO.Path]::GetTempPath() + '\' + $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -TimeoutSec 120

    # ── Step 2: SHA256 校验（fail-closed）──
    # 2026-08-24：校验文件缺失/下载失败/格式非法一律中止更新，不再跳过校验继续安装。
    # 原实现 catch 块只告警——发布资产缺 .sha256 时攻击者篡改下载源即可绕过完整性校验。
    Write-Host "  Download complete, verifying SHA256..."
    $sha256Url = $asset.browser_download_url + '.sha256'
    $sha256File = $tmp + '.sha256'
    try {
        Invoke-WebRequest -Uri $sha256Url -OutFile $sha256File -TimeoutSec 15 -ErrorAction Stop
    } catch {
        Write-Host "  Update FAILED: SHA256 checksum file not available ($sha256Url). Aborting (fail-closed)."
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        Remove-Item -Force $sha256File -ErrorAction SilentlyContinue
        exit 1
    }
    if (-not (Test-Path $sha256File) -or (Get-Item $sha256File).Length -eq 0) {
        Write-Host "  Update FAILED: SHA256 checksum file is empty. Aborting."
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        Remove-Item -Force $sha256File -ErrorAction SilentlyContinue
        exit 1
    }
    $expected = ((Get-Content $sha256File -First 1) -split '\s+' | Select-Object -First 1).ToLower()
    # 合法 sha256 = 64 位十六进制；否则视为格式损坏，拒绝安装
    if ($expected -notmatch '^[0-9a-f]{64}$') {
        Write-Host "  Update FAILED: SHA256 checksum file has invalid format. Aborting."
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        Remove-Item -Force $sha256File -ErrorAction SilentlyContinue
        exit 1
    }
    $actual = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) {
        Write-Host "  SHA256 verification FAILED! Aborting update."
        Write-Host "  Expected: $expected"
        Write-Host "  Actual:   $actual"
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        Remove-Item -Force $sha256File -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "  SHA256 verification passed"

    # ── Step 3: 解压并检查 tar 退出码 ──
    Write-Host "  Download complete, extracting..."
    $extractDir = [System.IO.Path]::GetTempPath() + '\xiaoda-agent-update'
    if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
    New-Item -ItemType Directory -Path $extractDir | Out-Null
    if (Get-Command tar -ErrorAction SilentlyContinue) {
        tar xzf $tmp -C $extractDir
        # 必须显式检查 $LASTEXITCODE：tar 解压失败（磁盘满、压缩包损坏）时
        # PowerShell 不会抛异常，但 $LASTEXITCODE 非 0。
        # 原bug：未检查导致后续 copy 把残缺文件写入安装目录，程序损坏。
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  Update FAILED: tar extraction failed with exit code $LASTEXITCODE"
            Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
            Remove-Item -Force $tmp -ErrorAction SilentlyContinue
            exit 1
        }
    } else {
        Write-Host "  Error: tar command not available. Windows 10 1803+ required for auto-update."
        exit 1
    }

    # ── Step 4: 校验候选目录关键文件齐全 ──
    $srcDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
    $updateSrc = if ($srcDir) { $srcDir.FullName } else { $extractDir }
    foreach ($file in $criticalFiles) {
        if (-not (Test-Path (Join-Path $updateSrc $file))) {
            Write-Host "  Update FAILED: candidate missing critical file: $file"
            Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
            Remove-Item -Force $tmp -ErrorAction SilentlyContinue
            exit 1
        }
    }
    Write-Host "  Candidate validated: all $($criticalFiles.Count) critical files present"

    # ── Step 5: 备份用户数据 + 整个安装目录 ──
    Write-Host "  Backing up configuration..."
    $backupDir = [System.IO.Path]::GetTempPath() + 'xiaoda-agent-backup-v' + $CurrentVersion
    if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }
    foreach ($item in $userDataItems) {
        $src = $env:USERPROFILE + '\.ai-agent\' + $item
        if (Test-Path $src) { Copy-Item -Recurse -Force $src $backupDir\ }
    }

    # 程序目录完整备份：复制失败时用于回滚，避免半更新状态损坏安装
    # CodeRabbit 审查：$programBackupReady 标志确保只有完整备份才能触发回滚，
    # 防止 Copy-Item 中途失败后 catch 块用不完整的备份恢复导致安装损坏
    $programBackupDir = [System.IO.Path]::GetTempPath() + 'xiaoda-agent-program-backup-v' + $CurrentVersion
    $programBackupReady = $false
    if (Test-Path $programBackupDir) { Remove-Item -Recurse -Force $programBackupDir }
    if (Test-Path $installDir) {
        Copy-Item -Recurse -Force $installDir $programBackupDir
        $programBackupReady = $true
        Write-Host "  Program directory backed up to $programBackupDir"
    }

    # ── Step 6: 停止旧进程并干净替换安装目录 ──
    Write-Host "  Installing update..."
    $proc = Get-Process -Name 'xiaoda-agent' -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "  Stopping running instance..."
        Stop-Process -Name 'xiaoda-agent' -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    # 先清后拷（2026-08-30 修复，与 Linux auto-update.sh 同语义）：仅删两个
    # dist 目录会让候选包中已移除的旧文件残留（vite hash 前端资源随升级越积
    # 越多）。候选包已通过关键文件校验且安装目录已完整备份，清空是可回滚的。
    # state 文件（.env 用户兜底配置 / .auto_update 更新开关）先摘出到临时区，
    # 拷贝后放回；.version 由候选包带入，校验全部通过后才覆写。
    # 注：清空若因文件占用失败会抛异常进入 catch 回滚（fail-closed），
    # 不允许半清半拷留下混合新旧文件的安装目录。
    $stateFiles = @('.env', '.auto_update')
    $stateStash = Join-Path $extractDir 'state-stash'
    New-Item -ItemType Directory -Path $stateStash -Force | Out-Null
    foreach ($sf in $stateFiles) {
        $stateSrc = Join-Path $installDir $sf
        if (Test-Path $stateSrc) { Move-Item -Force $stateSrc $stateStash }
    }
    Get-ChildItem -Path $installDir -Force | Remove-Item -Recurse -Force
    Get-ChildItem -Path $updateSrc | Copy-Item -Recurse -Force -Destination $installDir\
    foreach ($sf in $stateFiles) {
        $stashedFile = Join-Path $stateStash $sf
        $stateDest = Join-Path $installDir $sf
        if ((Test-Path $stashedFile) -and (-not (Test-Path $stateDest))) {
            Move-Item -Force $stashedFile $stateDest
        }
    }

    # ── Step 7: 校验安装后关键文件齐全 ──
    # xiaoda-agent.exe 是主程序入口，必须显式校验（也是版本写入的前置条件）
    if (-not (Test-Path ($installDir + '\xiaoda-agent.exe'))) {
        Write-Host "  Update FAILED: xiaoda-agent.exe missing. Restoring backup..."
        $rolledBack = $true
    } else {
        foreach ($file in $criticalFiles) {
            if (-not (Test-Path ($installDir + '\' + $file))) {
                Write-Host "  Update FAILED: install missing critical file: $file. Restoring backup..."
                $rolledBack = $true
                break
            }
        }
    }

    if ($rolledBack) {
        # 从程序目录备份完整恢复，避免半更新状态损坏安装
        if ($programBackupReady -and (Test-Path $programBackupDir)) {
            $restoredSrc = Join-Path $programBackupDir (Split-Path $installDir -Leaf)
            if (-not (Test-Path $restoredSrc)) { $restoredSrc = $programBackupDir }
            Remove-Item -Recurse -Force $installDir -ErrorAction SilentlyContinue
            Copy-Item -Recurse -Force $restoredSrc (Split-Path $installDir -Parent)
            Write-Host "  Program directory restored from backup."
        }
        # 用户数据也恢复（避免新版本格式不兼容）
        foreach ($item in $userDataItems) {
            $src = $backupDir + '\' + $item
            if (Test-Path $src) { Copy-Item -Recurse -Force $src ($env:USERPROFILE + '\.ai-agent\') }
        }
        Write-Host "  Backup restored. Please use setup.exe for manual update."
        exit 1
    }

    # ── Step 8: 恢复用户数据（成功路径）──
    foreach ($item in $userDataItems) {
        $src = $backupDir + '\' + $item
        if (Test-Path $src) { Copy-Item -Recurse -Force $src ($env:USERPROFILE + '\.ai-agent\') }
    }

    # ── Step 9: 仅在校验全部通过后写版本号 ──
    # 顺序至关重要：Test-Path 校验在前，Set-Content 在后。
    # 原bug：版本号先于校验写入，导致 .version 与实际程序不一致。
    Set-Content -Path $VerFile -Value $latest -NoNewline
    Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $backupDir -ErrorAction SilentlyContinue
    if ($programBackupReady -and (Test-Path $programBackupDir)) {
        Remove-Item -Recurse -Force $programBackupDir -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "  Update complete! v$latest"
    if ($updateMutexAcquired) { $updateMutex.ReleaseMutex() }
} catch {
    Write-Host "  Update check failed: $($_.Exception.Message)"
    # 兜底回滚：异常路径下若已备份程序目录，尝试恢复
    if (-not $rolledBack -and $programBackupReady -and (Test-Path $programBackupDir) -and (Test-Path $installDir)) {
        Write-Host "  Attempting rollback from program backup..."
        try {
            $restoredSrc = Join-Path $programBackupDir (Split-Path $installDir -Leaf)
            if (-not (Test-Path $restoredSrc)) { $restoredSrc = $programBackupDir }
            Remove-Item -Recurse -Force $installDir -ErrorAction SilentlyContinue
            Copy-Item -Recurse -Force $restoredSrc (Split-Path $installDir -Parent)
            Write-Host "  Rollback complete."
        } catch {
            Write-Host "  Rollback failed: $($_.Exception.Message)"
        }
    }
    # 自动更新失败不应阻塞启动（用户可下次再试或手动 setup.exe）
    if ($updateMutexAcquired) { $updateMutex.ReleaseMutex() }
    exit 0
}
