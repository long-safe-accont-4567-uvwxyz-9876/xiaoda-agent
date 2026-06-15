@echo off
setlocal

:: ============================================
::   Nahida Agent - Windows Launcher
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
echo   =     Nahida Agent            =
echo   ================================
echo.

:: Auto-update check (if enabled)
if exist "%~dp0auto-update.bat" (
    call "%~dp0auto-update.bat"
)

:: Find the executable
:: Onedir build: exe is either in same dir as this bat, or in dist\nahida-agent\
set "EXE_PATH="
if exist "%~dp0nahida-agent.exe" (
    set "EXE_PATH=%~dp0nahida-agent.exe"
) else if exist "%~dp0dist\nahida-agent\nahida-agent.exe" (
    set "EXE_PATH=%~dp0dist\nahida-agent\nahida-agent.exe"
) else (
    echo   [ERROR] nahida-agent.exe not found!
    echo   Looked in:
    echo     %~dp0nahida-agent.exe
    echo     %~dp0dist\nahida-agent\nahida-agent.exe
    echo.
    echo   Please check the installation path.
    goto :pause_exit
)

:: Change to the script directory
cd /d "%~dp0"

:: Start in Web mode by default (first-run will auto-trigger setup wizard)
echo   Starting Nahida Agent...
echo.
"%EXE_PATH%" --web

:: Check exit code
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Nahida Agent exited with code %errorlevel%
)

:pause_exit
echo.
pause
