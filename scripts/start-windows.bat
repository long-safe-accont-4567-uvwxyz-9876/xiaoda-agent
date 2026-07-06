@echo off
setlocal

:: ============================================
::   Xiaoda Agent - Windows Launcher
:: ============================================

:: Handle Ctrl+C gracefully
if "%~1"=="" goto :main
if /i "%~1"=="--web" goto :main
goto :usage

:usage
echo.
echo   Usage: start-windows.bat [--web]
echo.
echo   Options:
echo     --web    Start in Web UI mode
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

:: Start in Web mode by default (first-run will auto-trigger setup wizard)
echo   Starting Xiaoda Agent...
echo.

:: Launch browser once server is ready (background polling)
if exist "%~dp0open-browser.ps1" (
    start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-browser.ps1" -Port 8082
)

:: Run the main executable in desktop mode (pywebview native window)
"%EXE_PATH%" --desktop --port 8082

:: Check exit code
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Xiaoda Agent exited with code %errorlevel%
)

:pause_exit
echo.
pause
