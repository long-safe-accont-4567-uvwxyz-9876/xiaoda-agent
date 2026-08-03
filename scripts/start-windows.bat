@echo off
chcp 65001 >nul 2>&1
setlocal

:: ============================================
::   Xiaoda Agent - Windows Launcher
:: ============================================

:: Default: --web mode (system browser, lighter; avoids WebView2 GPU usage)
:: --desktop launches msedgewebview2.exe and may stutter on high-refresh screens
:: --desktop requires WebView2 Runtime; auto-fallback to --web if missing
set "LAUNCH_MODE=--web"
if "%~1"=="" goto :main
if /i "%~1"=="--web" goto :main
if /i "%~1"=="--desktop" goto :check_desktop
goto :usage

:check_desktop
rem Detect WebView2 Runtime (per-machine + per-user registry locations)
set "WEBVIEW2_OK="
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WEBVIEW2_OK=1"
reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WEBVIEW2_OK=1"
if defined WEBVIEW2_OK (
    set "LAUNCH_MODE=--desktop"
) else (
    echo   [WARN] WebView2 Runtime not found, falling back to Web mode
    echo   To use a native desktop window, install Microsoft Edge WebView2 Runtime:
    echo     https://developer.microsoft.com/microsoft-edge/webview2/
    echo.
    set "LAUNCH_MODE=--web"
)
goto :main

:usage
echo.
echo   Usage: start-windows.bat [--web ^| --desktop]
echo.
echo   Options:
echo     --web       Start in Web UI mode (default, opens system browser)
echo     --desktop   Start in Desktop mode (pywebview native window)
echo.
goto :eof

:main
echo.
echo   ================================
echo   =     Xiaoda Agent            =
echo   ================================
echo.

:: No update check on launch. Update is a separate action:
::   double-click the "Check Update" shortcut or run auto-update.bat
set "EXE_PATH="
if exist "%~dp0xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0xiaoda-agent.exe"
) else if exist "%~dp0dist\xiaoda-agent\xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0dist\xiaoda-agent\xiaoda-agent.exe"
) else (
    echo   [ERROR] xiaoda-agent.exe not found!
    echo   Looked in:
    echo     %~dp0xiaoda-agent.exe
    echo     %~dp0dist\xiaoda-agent\xiaoda-agent.exe
    echo.
    echo   Please check the installation path.
    goto :pause_exit
)

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

:: Force UTF-8 IO for the Python process (prevents UnicodeEncodeError under GBK)
set PYTHONIOENCODING=utf-8

:: Installer forces port 8082 (overrides inherited WEBUI_PORT from dev machine)
:: Dev: run `python agent.py --web --port 8080` directly, not via this bat
set "WEBUI_PORT=8082"

netstat -ano | findstr ":%WEBUI_PORT% " | findstr "LISTENING" >nul 2>nul
if %errorlevel% equ 0 (
    echo   [WARN] Port %WEBUI_PORT% is already in use!
    echo   Options:
    echo     1. Close the program using that port and retry
    echo     2. Or use another port: set WEBUI_PORT=8083 ^&^& start-windows.bat
    echo.
    goto :pause_exit
)

echo   Starting Xiaoda Agent...
echo.

if /i "%LAUNCH_MODE%"=="--web" (
    if exist "%~dp0open-browser.ps1" (
        start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-browser.ps1" -Port %WEBUI_PORT%
    )
)

:: Watchdog mode: auto-restart on freeze (>60s) or crash; stops after 20 restarts/600s
"%EXE_PATH%" watchdog --mode %LAUNCH_MODE:--=% --port %WEBUI_PORT% --log-file "%~dp0logs\watchdog.log"

if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Xiaoda Agent exited with code %errorlevel%
    echo.
    echo   Watchdog stopped auto-recovery. Possible causes: repeated crashes,
    echo   invalid API key, or network issue.
    echo   Run doctor.bat to diagnose, or check logs\watchdog.log.
)

:pause_exit
echo.
pause
