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
set "INSTALL_DIR=%~dp0.."
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
    "  Write-Host '  Download complete, extracting...'; " ^
    "  $extractDir = [System.IO.Path]::GetTempPath() + '\xiaoda-agent-update'; " ^
    "  if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }; " ^
    "  New-Item -ItemType Directory -Path $extractDir | Out-Null; " ^
    "  tar xzf $tmp -C $extractDir; " ^
    "  Write-Host '  Backing up configuration...'; " ^
    "  $backupDir = $installDir + '\.backup_v' + $curVer; " ^
    "  if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }; " ^
    "  foreach ($item in @('.env', 'config', 'credentials', 'data')) { " ^
    "    $src = $env:USERPROFILE + '\.ai-agent\' + $item; " ^
    "    if (Test-Path $src) { Copy-Item -Recurse -Force $src $backupDir\ }; " ^
    "  }; " ^
    "  Write-Host '  Installing update...'; " ^
    "  $srcDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1; " ^
    "  if ($srcDir) { " ^
    "    Get-ChildItem -Path $srcDir.FullName | Copy-Item -Recurse -Force -Destination $installDir\; " ^
    "  } else { " ^
    "    Get-ChildItem -Path $extractDir | Copy-Item -Recurse -Force -Destination $installDir\; " ^
    "  }; " ^
    "  foreach ($item in @('.env', 'config', 'credentials', 'data')) { " ^
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
