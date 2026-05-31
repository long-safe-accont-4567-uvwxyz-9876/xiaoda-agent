#!/bin/bash
set -e

AI_AGENT_DIR="/home/orangepi/ai-agent"
PYTHON="/home/orangepi/miniconda3/bin/python"

echo "============================================"
echo "  🤖 QQ AI Agent 启动脚本"
echo "============================================"
echo ""
echo "启动顺序:"
echo "  1. NoneBot2 (端口 8080)"
echo "  2. QQ Client + NapCat (WebUI 端口 6099)"
echo ""

# 1. Start NoneBot2
echo "🚀 步骤 1/2: 启动 NoneBot2..."
cd "$AI_AGENT_DIR"
$PYTHON bot.py &
BOT_PID=$!
sleep 3

# Check if NoneBot2 started
if kill -0 $BOT_PID 2>/dev/null; then
    echo "✅ NoneBot2 启动成功 (PID: $BOT_PID)"
else
    echo "❌ NoneBot2 启动失败"
    exit 1
fi

# 2. Start QQ with NapCat/LiteLoader
echo ""
echo "🚀 步骤 2/2: 启动 QQ (扫码登录)..."
echo ""

# Kill any existing QQ processes
pkill -f "/opt/QQ/qq" 2>/dev/null || true

# Start QQ (LiteLoader will auto-load NapCat)
DISPLAY=:0 /opt/QQ/qq --no-sandbox &
QQ_PID=$!
sleep 3

echo ""
echo "============================================"
echo "  ✅ 全部启动完成！"
echo ""
echo "  📱 打开浏览器访问: http://127.0.0.1:6099"
echo "  📱 扫码登录 QQ"
echo ""
echo "  NoneBot2 PID: $BOT_PID"
echo "  QQ PID:      $QQ_PID"
echo "============================================"
echo ""
echo "按 Ctrl+C 停止所有服务"

trap "kill $BOT_PID $QQ_PID 2>/dev/null; exit" INT TERM
wait