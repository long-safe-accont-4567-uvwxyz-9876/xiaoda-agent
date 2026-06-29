@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

:: ============================================
::   Xiaoda Agent - Doctor Self-Check
::   一键自检脚本 (零 API 调用, <2s)
::   双击运行即可
:: ============================================

:: Banner
echo.
echo   ==========================================
echo   ^|   Xiaoda Agent Doctor Self-Check       ^|
echo   ^|   零 API 调用, 2 秒内完成              ^|
echo   ==========================================
echo.

:: 解析参数 (支持 --json / --fix / 无参数)
set "ARGS="
if /i "%~1"=="--json" set "ARGS=--json"
if /i "%~1"=="json" set "ARGS=--json"
if /i "%~1"=="--fix" set "ARGS=--fix"
if /i "%~1"=="fix" set "ARGS=--fix"

:: 强制 UTF-8 输出编码 (防止中文 Windows GBK 报错)
set PYTHONIOENCODING=utf-8

:: 查找可执行文件
set "EXE_PATH="
if exist "%~dp0xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0xiaoda-agent.exe"
) else if exist "%~dp0dist\xiaoda-agent\xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0dist\xiaoda-agent\xiaoda-agent.exe"
) else if exist "%~dp0..\xiaoda-agent.exe" (
    set "EXE_PATH=%~dp0..\xiaoda-agent.exe"
)

:: 切换到脚本所在目录
cd /d "%~dp0"

if defined EXE_PATH (
    :: 打包后的 exe 模式
    echo   [i] 使用打包版本: !EXE_PATH!
    echo.
    "!EXE_PATH!" doctor !ARGS!
    set EXITCODE=%errorlevel%
) else (
    :: 开发模式: 直接用 python 运行
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set "PY_CMD=python"
    ) else (
        where py >nul 2>nul
        if %errorlevel% equ 0 (
            set "PY_CMD=py"
        ) else (
            echo   [ERROR] 未找到 xiaoda-agent.exe 也未找到 python
            echo.
            echo   请确认:
            echo     1. 已通过安装程序安装 Xiaoda Agent
            echo     2. 或在开发环境运行 (需要 python)
            echo.
            pause
            exit /b 1
        )
    )

    :: 查找 agent.py
    set "AGENT_PY=%~dp0..\agent.py"
    if not exist "!AGENT_PY!" set "AGENT_PY=%~dp0agent.py"
    if not exist "!AGENT_PY!" (
        echo   [ERROR] 未找到 agent.py
        echo   查找位置: !AGENT_PY!
        pause
        exit /b 1
    )

    echo   [i] 使用开发模式: !PY_CMD! !AGENT_PY!
    echo.
    "!PY_CMD!" "!AGENT_PY!" doctor !ARGS!
    set EXITCODE=%errorlevel%
)

echo.
if !EXITCODE! equ 0 (
    echo   [OK] 自检全部通过 ✓
) else (
    echo   [FAIL] 自检发现问题, 退出码 !EXITCODE!
    echo.
    echo   提示:
    echo     · 运行 doctor.bat fix  可尝试自动修复
    echo     · 运行 doctor.bat json 可获取 JSON 格式报告
)
echo.
pause
exit /b !EXITCODE!
