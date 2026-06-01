#!/bin/bash
echo "=== 纳西妲 AI Agent 健康检查 ==="
echo ""

echo "[1] KIOXIA U盘挂载"
if mountpoint -q /media/orangepi/KIOXIA; then
    echo "  ✅ 已挂载"
else
    echo "  ❌ 未挂载！请检查U盘连接"
    exit 1
fi

echo "[2] 数据库目录"
if [ -d "/media/orangepi/KIOXIA/nahida-data/db" ]; then
    DB_SIZE=$(du -sh /media/orangepi/KIOXIA/nahida-data/db/ | cut -f1)
    echo "  ✅ 存在 (${DB_SIZE})"
else
    echo "  ❌ 不存在！"
    exit 1
fi

echo "[3] 数据库文件"
if [ -f "/media/orangepi/KIOXIA/nahida-data/db/agent.db" ]; then
    echo "  ✅ agent.db 存在"
else
    echo "  ❌ agent.db 不存在！"
    exit 1
fi

echo "[4] Agent 服务状态"
STATUS=$(systemctl is-active qq-agent.service 2>/dev/null)
if [ "$STATUS" = "active" ]; then
    PID=$(pgrep -f qq_bot_adapter | head -1)
    MEM=$(ps -o rss= -p $PID 2>/dev/null | awk '{printf "%.0fMB", $1/1024}')
    echo "  ✅ 运行中 (PID: $PID, 内存: $MEM)"
else
    echo "  ❌ 未运行 (状态: $STATUS)"
fi

echo "[5] WebSocket 连接"
if journalctl -u qq-agent.service --since "5 min ago" --no-pager 2>/dev/null | grep -q "心跳维持启动"; then
    echo "  ✅ WebSocket 已连接"
else
    RECENT=$(journalctl -u qq-agent.service --since "5 min ago" --no-pager 2>/dev/null | grep -c "on_closed\|on_error\|Session timed out")
    if [ "$RECENT" -gt 3 ]; then
        echo "  ⚠️  WebSocket 不稳定 (${RECENT} 次断连)"
    else
        echo "  ℹ️  无法确认（可能刚启动）"
    fi
fi

echo "[6] 日志目录"
if [ -d "/media/orangepi/KIOXIA/nahida-data/logs" ]; then
    LOG_COUNT=$(ls /media/orangepi/KIOXIA/nahida-data/logs/*.json 2>/dev/null | wc -l)
    echo "  ✅ 存在 (${LOG_COUNT} 个日志文件)"
else
    echo "  ❌ 不存在！"
fi

echo ""
echo "=== 检查完成 ==="
