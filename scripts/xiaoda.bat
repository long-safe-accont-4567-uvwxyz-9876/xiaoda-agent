@echo off
chcp 65001 >nul 2>&1
setlocal

:: ============================================
::   Xiaoda Agent - CLI 入口
::   - 双击本文件：进入 CLI 交互界面
::   - 安装包已把本目录加入用户 PATH，cmd 中直接输入 `xiaoda` 即可进入 CLI
:: ============================================

set "EXE_PATH=%~dp0xiaoda-agent.exe"
if not exist "%EXE_PATH%" (
    echo   [ERROR] xiaoda-agent.exe not found: %EXE_PATH%
    echo.
    echo   Please check the installation path.
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0"

:: Force UTF-8 IO for the Python process (prevents UnicodeEncodeError under GBK)
set PYTHONIOENCODING=utf-8

:: 进入 CLI 交互界面
"%EXE_PATH%" --cli