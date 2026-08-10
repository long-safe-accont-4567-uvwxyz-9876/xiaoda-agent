#!/bin/bash
# 数据路径统一从 config.py 运行时解析读取（U盘挂载/降级/默认值由
# _resolve_data_path 处理），脚本不硬编码任何盘符路径。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PY_BIN="$PROJECT_ROOT/.venv/bin/python"
[ -x "$PY_BIN" ] || PY_BIN=python3
eval "$(cd "$PROJECT_ROOT" && "$PY_BIN" -c '
import os
from config import DATA_DIR, LOG_DIR
# 判定是否正在使用外置盘：仅当显式配置 KIOXIA_DATA_DIR 且 DATA_DIR
# 落在该路径下（_resolve_data_path 已确认挂载成功）才算使用外置盘
kioxia_env = os.getenv("KIOXIA_DATA_DIR", "")
using_kioxia = bool(kioxia_env) and str(DATA_DIR).startswith(kioxia_env)
print(f"DB_DIR={DATA_DIR}")
print(f"LOG_DIR={LOG_DIR}")
print(f"USING_KIOXIA={using_kioxia}")
')"

echo "=== 小妲 AI Agent 健康检查 ==="
echo ""

echo "[1] KIOXIA U盘挂载"
if [ "$USING_KIOXIA" = "True" ]; then
    echo "  ✅ 已挂载，数据库使用外置盘 ($DB_DIR)"
else
    echo "  ❌ 未挂载！数据库已降级到系统盘"
    exit 1
fi

echo "[2] 数据库目录"
if [ -d "$DB_DIR" ]; then
    DB_SIZE=$(du -sh "$DB_DIR/" | cut -f1)
    echo "  ✅ 存在 (${DB_SIZE})"
else
    echo "  ❌ 不存在！"
    exit 1
fi

echo "[3] 数据库文件"
if [ -f "$DB_DIR/agent.db" ]; then
    echo "  ✅ agent.db 存在"
else
    echo "  ❌ agent.db 不存在！"
    exit 1
fi

echo "[4] Agent 服务状态"
STATUS=$(systemctl is-active xiaoda-agent.service 2>/dev/null)
if [ "$STATUS" = "active" ]; then
    PID=$(pgrep -f qq_bot_adapter | head -1)
    if [ -n "$PID" ]; then
        MEM=$(ps -o rss= -p $PID 2>/dev/null | awk '{printf "%.0fMB", $1/1024}')
        echo "  ✅ 运行中 (PID: $PID, 内存: $MEM)"
    else
        echo "  ⚠️  运行中但无法获取 PID"
    fi
else
    echo "  ❌ 未运行 (状态: $STATUS)"
fi

echo "[5] WebSocket 连接"
if journalctl -u xiaoda-agent.service --since "5 min ago" --no-pager 2>/dev/null | grep -q "心跳维持启动"; then
    echo "  ✅ WebSocket 已连接"
else
    RECENT=$(journalctl -u xiaoda-agent.service --since "5 min ago" --no-pager 2>/dev/null | grep -c "on_closed\|on_error\|Session timed out")
    if [ "$RECENT" -gt 3 ]; then
        echo "  ⚠️  WebSocket 不稳定 (${RECENT} 次断连)"
    else
        echo "  ℹ️  无法确认（可能刚启动）"
    fi
fi

echo "[6] 日志目录"
if [ -d "$LOG_DIR" ]; then
    LOG_COUNT=$(find "$LOG_DIR" -maxdepth 1 -type f -name '*.json' -printf '.' | wc -c)
    echo "  ✅ 存在 (${LOG_COUNT} 个日志文件)"
else
    echo "  ❌ 不存在！"
fi

echo ""
echo "=== 检查完成 ==="
