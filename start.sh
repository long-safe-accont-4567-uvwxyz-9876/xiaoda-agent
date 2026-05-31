#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    echo ""
    echo "  🌿 检测到首次运行，启动配置向导..."
    echo ""
    python3 setup_wizard.py
    if [ $? -ne 0 ]; then
        echo ""
        echo "  ⚠ 配置向导未完成，请运行 python3 setup_wizard.py 重新配置"
        echo ""
        exit 1
    fi
fi

MIMO_KEY=$(grep -E "^MIMO_API_KEY=" .env 2>/dev/null | cut -d'=' -f2-)
if [ -z "$MIMO_KEY" ]; then
    echo ""
    echo "  ⚠ MIMO_API_KEY 未配置！"
    echo "  请运行 python3 setup_wizard.py 完成配置"
    echo ""
    read -p "  是否现在运行配置向导？[Y/n] " choice
    case "$choice" in
        n|N) exit 1 ;;
        *) python3 setup_wizard.py ;;
    esac
fi

echo ""
echo "  🌿 正在启动纳西妲 Agent 服务..."
echo ""

if command -v systemctl &>/dev/null; then
    STATUS=$(systemctl is-active qq-agent 2>/dev/null)
    if [ "$STATUS" != "active" ]; then
        echo "  启动 QQ Bot 服务..."
        sudo systemctl start qq-agent 2>/dev/null
        sleep 2
        STATUS=$(systemctl is-active qq-agent 2>/dev/null)
        if [ "$STATUS" = "active" ]; then
            echo "  ✅ QQ Bot 服务已启动"
        else
            echo "  ℹ️  QQ Bot 服务未启动（CLI 可正常使用）"
        fi
    else
        echo "  ✅ QQ Bot 服务已在运行"
    fi
fi

echo ""
echo "  启动 CLI 交互界面..."
echo ""

PYTHON="${PYTHON:-python3}"
exec "$PYTHON" cli.py