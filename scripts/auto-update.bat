@echo off
setlocal enabledelayedexpansion

:: ============================================
::   Xiaoda Agent - Auto-Update Script
::   Checks GitHub Release for new versions
:: ============================================

if defined GITHUB_REPO (
    set "REPO=%GITHUB_REPO%"
) else (
    set "REPO=long-safe-accont-4567-uvwxyz-9876/xiaoda-agent"
)
set "INSTALL_DIR=%~dp0"
:: Remove trailing backslash for consistent path joining
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"
set "VERSION_FILE=%INSTALL_DIR%\.version"
set "AUTO_UPDATE_FLAG=%INSTALL_DIR%\.auto_update"

:: Check if auto-update is enabled
if not exist "%AUTO_UPDATE_FLAG%" goto :eof

:: Get current version
set "CURRENT_VERSION="
if exist "%VERSION_FILE%" (
    set /p CURRENT_VERSION=<"%VERSION_FILE%"
)

:: Check for updates using PowerShell
echo   Checking for updates...

powershell -NoProfile -Command ^
    "$repo='%REPO%'; " ^
    "$curVer='%CURRENT_VERSION%'; " ^
    "$flagFile='%AUTO_UPDATE_FLAG%'; " ^
    "$verFile='%VERSION_FILE%'; " ^
    "$installDir='%INSTALL_DIR%'; " ^
    "try { " ^
    "  $release = Invoke-RestMethod -Uri \"https://api.github.com/repos/$repo/releases/latest\" -TimeoutSec 10; " ^
    "  $latest = $release.tag_name -replace '^v',''; " ^
    "  if ($latest -eq $curVer) { Write-Host '  Already up to date v' + $latest; exit 0 }; " ^
    "  Write-Host '  New version available: v' + $latest + ' (current: v' + $curVer + ')'; " ^
    "$asset = $release.assets | Where-Object { $_.name -like '*windows-x64*.tar.gz' } | Select-Object -First 1; " ^
    "  if (-not $asset) { Write-Host '  No Windows installer found, skipping'; exit 0 }; " ^
    "  Write-Host '  Downloading ' + $asset.name + ' ...'; " ^
    "  $tmp = [System.IO.Path]::GetTempPath() + '\' + $asset.name; " ^
    "  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -TimeoutSec 120; " ^
    "  Write-Host '  Download complete, verifying SHA256...'; " ^
    "  $sha256Url = $asset.browser_download_url + '.sha256'; " ^
    "  $sha256File = $tmp + '.sha256'; " ^
    "  $sha256Ok = $false; " ^
    "  try { " ^
    "    Invoke-WebRequest -Uri $sha256Url -OutFile $sha256File -TimeoutSec 15 -ErrorAction Stop; " ^
    "    $expected = (Get-Content $sha256File -First 1) -split '\s+' | Select-Object -First 1; " ^
    "    $actual = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLower(); " ^
    "    if ($expected -ne $actual) { " ^
    "      Write-Host '  SHA256 verification FAILED! Aborting update.'; " ^
    "      Write-Host ('  Expected: ' + $expected); " ^
    "      Write-Host ('  Actual:   ' + $actual); " ^
    "      Remove-Item -Force $tmp -ErrorAction SilentlyContinue; " ^
    "      Remove-Item -Force $sha256File -ErrorAction SilentlyContinue; " ^
    "      exit 1; " ^
    "    }; " ^
    "    Write-Host '  SHA256 verification passed'; " ^
    "    $sha256Ok = $true; " ^
    "  } catch { " ^
    "    if (-not $sha256Ok) { Write-Host '  Warning: SHA256 file not found, skipping verification' }; " ^
    "  }; " ^
    "  Write-Host '  Download complete, extracting...'; " ^
    "  $extractDir = [System.IO.Path]::GetTempPath() + '\xiaoda-agent-update'; " ^
    "  if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }; " ^
    "  New-Item -ItemType Directory -Path $extractDir | Out-Null; " ^
    "  if (Get-Command tar -ErrorAction SilentlyContinue) { tar xzf $tmp -C $extractDir } else { Write-Host '  Error: tar command not available. Windows 10 1803+ required for auto-update.'; exit 1 }; " ^
    "  Write-Host '  Backing up configuration...'; " ^
    "  $backupDir = [System.IO.Path]::GetTempPath() + 'xiaoda-agent-backup-v' + $curVer; " ^
    "  if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }; " ^
    "  foreach ($item in @('.env', 'config', 'credentials', 'data', 'stickers', 'xiaoli-stickers', 'agent-stickers', 'media', 'voice_refs', 'files', 'memory_state', 'plugins')) { " ^
    "    $src = $env:USERPROFILE + '\.ai-agent\' + $item; " ^
    "    if (Test-Path $src) { Copy-Item -Recurse -Force $src $backupDir\ }; " ^
    "  }; " ^
    "  Write-Host '  Installing update...'; " ^
    "  $srcDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1; " ^
    "  $updateSrc = if ($srcDir) { $srcDir.FullName } else { $extractDir }; " ^
    "  $proc = Get-Process -Name 'xiaoda-agent' -ErrorAction SilentlyContinue; " ^
    "  if ($proc) { Write-Host '  Stopping running instance...'; Stop-Process -Name 'xiaoda-agent' -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }; " ^
    "  Remove-Item -Recurse -Force ($installDir + '\_internal\web\dist') -ErrorAction SilentlyContinue; " ^
    "  Remove-Item -Recurse -Force ($installDir + '\web\dist') -ErrorAction SilentlyContinue; " ^
    "  Get-ChildItem -Path $updateSrc | Copy-Item -Recurse -Force -Destination $installDir\; " ^
    "  foreach ($item in @('.env', 'config', 'credentials', 'data', 'stickers', 'xiaoli-stickers', 'agent-stickers', 'media', 'voice_refs', 'files', 'memory_state', 'plugins')) { " ^
    "    $src = $backupDir + '\' + $item; " ^
    "    if (Test-Path $src) { Copy-Item -Recurse -Force $src ($env:USERPROFILE + '\.ai-agent\') }; " ^
    "  }; " ^
    "  Set-Content -Path $verFile -Value $latest -NoNewline; " ^
    "  Remove-Item -Recurse -Force $extractDir -ErrorAction SilentlyContinue; " ^
    "  Remove-Item -Force $tmp -ErrorAction SilentlyContinue; " ^
    "  Remove-Item -Recurse -Force $backupDir -ErrorAction SilentlyContinue; " ^
    "  Write-Host ''; " ^
    "  Write-Host '  Update complete! v' + $latest; " ^
    "} catch { " ^
    "  Write-Host '  Update check failed:' $_.Exception.Message; " ^
    "  exit 0; " ^
    "}" 2>nul

:: Ensure .auto_update flag file exists
if not exist "%AUTO_UPDATE_FLAG%" type nul > "%AUTO_UPDATE_FLAG%"

goto :eof