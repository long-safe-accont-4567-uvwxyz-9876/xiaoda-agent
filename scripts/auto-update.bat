@echo off
setlocal enabledelayedexpansion

:: ============================================
::   Nahida Agent - Auto-Update Script
::   Checks GitHub Release for new versions
:: ============================================

set "REPO=liu-runfei/nahida-agent"
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
    "  $asset = $release.assets | Where-Object { $_.name -like '*windows-x64*' } | Select-Object -First 1; " ^
    "  if (-not $asset) { Write-Host '  No Windows installer found, skipping'; exit 0 }; " ^
    "  Write-Host '  Downloading ' + $asset.name + ' ...'; " ^
    "  $tmp = [System.IO.Path]::GetTempPath() + '\' + $asset.name; " ^
    "  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -TimeoutSec 120; " ^
    "  Write-Host '  Download complete, extracting...'; " ^
    "  $extractDir = [System.IO.Path]::GetTempPath() + '\nahida-agent-update'; " ^
    "  if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }; " ^
    "  Expand-Archive -Path $tmp -DestinationPath $extractDir -Force; " ^
    "  Write-Host '  Backing up configuration...'; " ^
    "  $backupDir = $installDir + '\.backup_v' + $curVer; " ^
    "  if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }; " ^
    "  foreach ($item in @('.env', 'config', 'credentials', 'data')) { " ^
    "    $src = $installDir + '\' + $item; " ^
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
    "    if (Test-Path $src) { Copy-Item -Recurse -Force $src $installDir\ }; " ^
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

goto :eof
