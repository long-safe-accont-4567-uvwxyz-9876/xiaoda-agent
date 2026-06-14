#!/bin/bash
cd /home/orangepi/ai-agent

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

if [ "$MODE" = "web" ]; then
    echo "正在启动纳西妲 Agent Web UI..."
    sudo systemctl start qq-agent
    sleep 2

    STATUS=$(sudo systemctl is-active qq-agent)
    if [ "$STATUS" = "active" ]; then
        echo "QQ Bot 服务已启动 ✓"
    else
        echo "QQ Bot 服务启动失败，请检查: sudo journalctl -u qq-agent"
    fi

    echo ""
    PORT_ARG=""
    if [ -n "$PORT" ]; then
        PORT_ARG="--port $PORT"
    fi
    exec /home/orangepi/miniconda3/bin/python agent.py --web $PORT_ARG
else
    echo "正在启动纳西妲 Agent 服务..."
    sudo systemctl start qq-agent
    sleep 2

    STATUS=$(sudo systemctl is-active qq-agent)
    if [ "$STATUS" = "active" ]; then
        echo "QQ Bot 服务已启动 ✓"
    else
        echo "QQ Bot 服务启动失败，请检查: sudo journalctl -u qq-agent"
    fi

    echo ""
    echo "启动 CLI 交互界面..."
    echo ""
    exec /home/orangepi/miniconda3/bin/python cli.py
fi
