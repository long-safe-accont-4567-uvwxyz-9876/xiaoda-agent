@echo off
setlocal

:: ============================================
::   Xiaoda Agent - Windows Launcher
:: ============================================

:: Handle Ctrl+C gracefully
:: 默认 --web 模式（用系统浏览器，更轻量，避免 WebView2 子进程吃 GPU）
:: --desktop 模式会拉起 msedgewebview2.exe 子进程，在高刷新率屏幕上可能卡顿
set "LAUNCH_MODE=--web"
if "%~1"=="" goto :main
if /i "%~1"=="--web" goto :main
if /i "%~1"=="--desktop" (
    set "LAUNCH_MODE=--desktop"
    goto :main
)
goto :usage

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
:: Banner
echo.
echo   ================================
echo   =     Xiaoda Agent            =
echo   ================================
echo.

:: Auto-update check (if enabled)
if exist "%~dp0auto-update.bat" (
    call "%~dp0auto-update.bat"
)

:: Find the executable
:: Onedir build: exe is either in same dir as this bat, or in dist\xiaoda-agent\
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

:: Change to the script directory
cd /d "%~dp0"

:: Force UTF-8 output encoding (prevents UnicodeEncodeError with GBK on Chinese Windows)
set PYTHONIOENCODING=utf-8

:: Use WEBUI_PORT environment variable with fallback to 8082
if not defined WEBUI_PORT set "WEBUI_PORT=8082"

:: Start in Web mode by default (first-run will auto-trigger setup wizard)
echo   Starting Xiaoda Agent...
echo.

:: Launch browser once server is ready (only in --web mode; --desktop uses pywebview native window)
if /i "%LAUNCH_MODE%"=="--web" (
    if exist "%~dp0open-browser.ps1" (
        start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-browser.ps1" -Port %WEBUI_PORT%
    )
)

:: Run via watchdog (auto-restart on freeze/crash)
:: 看门狗模式：卡死超过 60s 或进程崩溃自动重启；20次/10分钟超限后停止自动恢复
"%EXE_PATH%" watchdog --mode %LAUNCH_MODE:--=% --port %WEBUI_PORT% --log-file "%~dp0logs\watchdog.log"

:: Check exit code
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Xiaoda Agent exited with code %errorlevel%
)

:pause_exit
echo.
pause