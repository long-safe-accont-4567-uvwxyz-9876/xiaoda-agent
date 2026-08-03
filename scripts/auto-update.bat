@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

:: ============================================
::   Xiaoda Agent - Auto-Update Script
::   Checks GitHub Release for new versions
::   Delegates logic to auto-update.ps1
::   (avoids cmd escaping issues with inline PowerShell)
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

:: Manual update script: runs only when user double-clicks this file
::   (or the "Check Update" shortcut). The main launcher does NOT call
::   this script. Launch and update are separate operations.

:: Get current version
set "CURRENT_VERSION="
if exist "%VERSION_FILE%" (
    set /p CURRENT_VERSION=<"%VERSION_FILE%"
)

:: Check for updates via PowerShell (delegated to .ps1 to avoid cmd escaping hell)
echo   Checking for updates...

set "PS1_PATH=%~dp0auto-update.ps1"
if not exist "%PS1_PATH%" (
    echo   [ERROR] auto-update.ps1 not found: %PS1_PATH%
    goto :pause_end
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_PATH%" ^
    -Repo "%REPO%" ^
    -CurrentVersion "%CURRENT_VERSION%" ^
    -FlagFile "%AUTO_UPDATE_FLAG%" ^
    -VerFile "%VERSION_FILE%" ^
    -InstallDir "%INSTALL_DIR%"

:pause_end
echo.
pause
goto :eof
