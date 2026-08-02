@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

:: ============================================
::   Xiaoda Agent - Auto-Update Script
::   Checks GitHub Release for new versions
::   Delegates logic to auto-update.ps1 (avoids cmd escaping issues with inline PowerShell)
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

:: 手动更新脚本：用户双击本文件（或「检查更新」快捷方式）时才执行
:: 启动主程序时不再自动调用本脚本 —— 启动与更新是两个独立操作

:: Get current version
set "CURRENT_VERSION="
if exist "%VERSION_FILE%" (
    set /p CURRENT_VERSION=<"%VERSION_FILE%"
)

:: Check for updates using PowerShell (delegated to .ps1 to avoid cmd escaping hell)
echo   正在检查更新...

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
