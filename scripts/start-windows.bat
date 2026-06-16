:: ================================================================
:: Nahida Agent Windows 启动脚本
:: ================================================================
@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

echo.
echo   ********************************************************
echo   *          Nahida Agent - Windows 启动脚本              *
echo   ********************************************************
echo.

:: Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

:: Set encoding to UTF-8
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

:: Check for .env and copy from .env.example if needed
if not exist ".env" (
    if exist ".env.example" (
        echo.
        echo   [!] .env 文件不存在，正在从 .env.example 复制...
        copy ".env.example" ".env" >nul
        echo   [OK] 已创建 .env 文件，请根据需要编辑配置
    )
)

:: Determine executable name
if exist "nahida-agent.exe" (
    set "EXE=nahida-agent.exe"
) else if exist "agent.py" (
    set "EXE=python agent.py"
) else (
    echo   [ERROR] 找不到 nahida-agent.exe 或 agent.py
    pause
    exit /b 1
)

echo.
echo   [*] 启动 Nahida Agent...
echo   [*] 可执行文件: %EXE%
echo   [*] 工作目录: %CD%
echo.

:: Launch browser once server is ready (background polling)
start "" powershell -NoProfile -Command "while($true){try{$r=Invoke-WebRequest -Uri 'http://localhost:8082/api/v1/setup/first-run' -UseBasicParsing -TimeoutSec 2;if($r.StatusCode -eq 200){Start-Process 'http://localhost:8082/#/setup';break}}catch{};Start-Sleep -Seconds 1}"

:: Run the main executable
%EXE% --web

echo.
echo   [*] Nahida Agent 已退出
pause
