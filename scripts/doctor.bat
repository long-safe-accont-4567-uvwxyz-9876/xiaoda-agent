@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

:: ============================================
::   Xiaoda Agent - Doctor Self-Check
::   Zero API calls, completes in <2s
::   Double-click to run
:: ============================================

echo.
echo   ==========================================
echo   ^|   Xiaoda Agent Doctor Self-Check       ^|
echo   ^|   Zero API calls, finishes in ~2s      ^|
echo   ==========================================
echo.

:: Parse args (--json / --fix / none)
set "ARGS="
if /i "%~1"=="--json" set "ARGS=--json"
if /i "%~1"=="json" set "ARGS=--json"
if /i "%~1"=="--fix" set "ARGS=--fix"
if /i "%~1"=="fix" set "ARGS=--fix"

:: Force UTF-8 IO for the Python process (prevents errors on Chinese Windows)
set PYTHONIOENCODING=utf-8

:: Locate the executable
set "EXE_PATH="
if exist "%~dp0xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0xiaoda-agent.exe"
) else if exist "%~dp0dist\xiaoda-agent\xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0dist\xiaoda-agent\xiaoda-agent.exe"
) else if exist "%~dp0..\xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0..\xiaoda-agent.exe"
)

cd /d "%~dp0"

if not defined EXE_PATH goto :dev_mode

:: Packaged exe mode
echo   [i] Using packaged build: !EXE_PATH!
echo.
"!EXE_PATH!" doctor !ARGS!
set EXITCODE=!errorlevel!
goto :doctor_done

:dev_mode
:: Dev mode: run via python directly
:: Note: must use !errorlevel! not %errorlevel%
::   cmd expands %errorlevel% at parse time for the whole if-else block,
::   so the second `where py` result would be overwritten by the first
::   `where python` errorlevel. Delayed expansion !errorlevel! reads at
::   runtime to reflect the real exit code of the previous command.
where python >nul 2>nul
if !errorlevel! equ 0 (
    set "PY_CMD=python"
) else (
    where py >nul 2>nul
    if !errorlevel! equ 0 (
        set "PY_CMD=py"
    ) else (
        echo   [ERROR] Neither xiaoda-agent.exe nor python was found
        echo.
        echo   Please confirm:
        echo     1. Xiaoda Agent is installed via the installer
        echo     2. Or run in dev environment (requires python)
        echo.
        pause
        exit /b 1
    )
)

:: Locate agent.py
set "AGENT_PY=%~dp0..\agent.py"
if not exist "!AGENT_PY!" set "AGENT_PY=%~dp0agent.py"
if not exist "!AGENT_PY!" (
    echo   [ERROR] agent.py not found
    echo   Searched: !AGENT_PY!
    pause
    exit /b 1
)

echo   [i] Using dev mode: !PY_CMD! !AGENT_PY!
echo.
"!PY_CMD!" "!AGENT_PY!" doctor !ARGS!
set EXITCODE=!errorlevel!

:doctor_done
echo.
if !EXITCODE! equ 0 (
    echo   [OK] All self-checks passed
) else (
    echo   [FAIL] Self-check found issues, exit code !EXITCODE!
    echo.
    echo   Tips:
    echo     - Run "doctor.bat fix"  to attempt auto-repair
    echo     - Run "doctor.bat json" for a JSON report
)
echo.
pause
exit /b !EXITCODE!
