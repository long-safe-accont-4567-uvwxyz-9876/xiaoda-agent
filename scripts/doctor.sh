#!/usr/bin/env bash
# ============================================
#   Xiaoda Agent - Doctor Self-Check
#   一键自检脚本 (零 API 调用, <2s)
#   用法: ./doctor.sh [json|fix]
# ============================================
set -e

# Banner
echo
echo "  =========================================="
echo "  |   Xiaoda Agent Doctor Self-Check       |"
echo "  |   零 API 调用, 2 秒内完成              |"
echo "  =========================================="
echo

# 解析参数
ARGS=""
if [ "$1" = "json" ] || [ "$1" = "--json" ]; then ARGS="--json"; fi
if [ "$1" = "fix" ] || [ "$1" = "--fix" ]; then ARGS="--fix"; fi

# 切换到脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 查找可执行文件
EXE_PATH=""
if [ -f "./xiaoda-agent" ]; then
    EXE_PATH="./xiaoda-agent"
elif [ -f "../xiaoda-agent" ]; then
    EXE_PATH="../xiaoda-agent"
fi

if [ -n "$EXE_PATH" ]; then
    echo "  [i] 使用打包版本: $EXE_PATH"
    echo
    "$EXE_PATH" doctor $ARGS
    EXITCODE=$?
else
    # 开发模式: 查找 agent.py
    AGENT_PY="./agent.py"
    if [ ! -f "$AGENT_PY" ]; then
        AGENT_PY="../agent.py"
    fi
    if [ ! -f "$AGENT_PY" ]; then
        echo "  [ERROR] 未找到 xiaoda-agent 可执行文件或 agent.py"
        exit 1
    fi

    # 查找 python
    if command -v python3 >/dev/null 2>&1; then
        PY_CMD="python3"
    elif command -v python >/dev/null 2>&1; then
        PY_CMD="python"
    else
        echo "  [ERROR] 未找到 python3 / python"
        exit 1
    fi

    echo "  [i] 使用开发模式: $PY_CMD $AGENT_PY"
    echo
    $PY_CMD "$AGENT_PY" doctor $ARGS
    EXITCODE=$?
fi

echo
if [ $EXITCODE -eq 0 ]; then
    echo "  [OK] 自检全部通过 ✓"
else
    echo "  [FAIL] 自检发现问题, 退出码 $EXITCODE"
    echo
    echo "  提示:"
    echo "    · 运行 ./doctor.sh fix  可尝试自动修复"
    echo "    · 运行 ./doctor.sh json 可获取 JSON 格式报告"
fi
exit $EXITCODE