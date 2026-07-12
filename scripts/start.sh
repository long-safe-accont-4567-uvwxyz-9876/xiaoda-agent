#!/bin/bash
# ── 小妲 Agent 启动脚本 ──
# 自动检测安装目录，支持 --web / --port 参数

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Parse arguments
MODE="cli"
PORT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --web) MODE="web"; shift ;;
        --port) PORT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Detect Python
if [ -d "$SCRIPT_DIR/.venv" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "ERROR: Python not found. Please install Python 3.11+"
    exit 1
fi

if [ "$MODE" = "web" ]; then
    echo "正在启动小妲 Agent Web UI..."
    # 尝试启动 QQ Bot 服务（如果已配置 systemd）
    if systemctl list-unit-files | grep -q qq-agent; then
        sudo systemctl start qq-agent 2>/dev/null || true
        sleep 2
        STATUS=$(sudo systemctl is-active qq-agent 2>/dev/null || echo "unknown")
        if [ "$STATUS" = "active" ]; then
            echo "QQ Bot 服务已启动 ✓"
        else
            echo "QQ Bot 服务未启动，请检查: sudo journalctl -u qq-agent"
        fi
    fi

    echo ""
    PORT_ARG=""
    if [ -n "$PORT" ]; then
        PORT_ARG="--port $PORT"
    fi
    exec $PYTHON agent.py --web $PORT_ARG
else
    echo "正在启动小妲 Agent CLI..."
    # 尝试启动 QQ Bot 服务
    if systemctl list-unit-files | grep -q qq-agent; then
        sudo systemctl start qq-agent 2>/dev/null || true
    fi
    echo ""
    exec $PYTHON cli.py
fi
