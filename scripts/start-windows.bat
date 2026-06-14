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

:: Check if executable exists
if not exist "%~dp0dist\nahida-agent\nahida-agent.exe" (
    echo   [ERROR] nahida-agent.exe not found!
    echo   Expected location: %~dp0dist\nahida-agent\nahida-agent.exe
    echo.
    echo   Please build the project first or check the installation path.
    goto :pause_exit
)

:: Change to the script directory
cd /d "%~dp0"

:: Start in the appropriate mode
if /i "%~1"=="--web" (
    echo   Starting Nahida Agent in Web mode...
    echo.
    dist\nahida-agent\nahida-agent.exe --web
) else (
    echo   Starting Nahida Agent in CLI mode...
    echo   (Use --web flag for Web UI mode)
    echo.
    dist\nahida-agent\nahida-agent.exe
)

:: Check exit code
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Nahida Agent exited with code %errorlevel%
)

:pause_exit
echo.
pause
