#!/bin/bash
# ensure-bridge.sh — 确保 coze-bridge 在 agent 启动前连接成功
# 用法: ensure-bridge.sh [max_retries] [retry_delay_sec]

BRIDGE_BIN="${COZE_BRIDGE_BIN:-$HOME/.coze/bridge/bin/coze-bridge}"
MAX_RETRIES=${1:-3}
RETRY_DELAY=${2:-8}

# systemd 环境缺少用户级变量，需要手动设置
export HOME="${HOME:-/home/orangepi}"
export USER="${USER:-orangepi}"
export PATH="${COZE_NODE_PATH:-$HOME/.trae-cn-server/binaries/node/versions/22.22.3/bin}:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

# 如果 bridge 已在运行，直接退出
STATUS=$("$BRIDGE_BIN" status 2>/dev/null)
if echo "$STATUS" | grep -q '"running": true'; then
    echo "ensure-bridge: already running"
    exit 0
fi

# 清理残留状态
"$BRIDGE_BIN" stop 2>/dev/null
sleep 1

# 重试连接
for i in $(seq 1 "$MAX_RETRIES"); do
    echo "ensure-bridge: connect attempt $i/$MAX_RETRIES"
    "$BRIDGE_BIN" connect 2>&1

    # 等待 daemon 完成握手
    sleep "$RETRY_DELAY"

    # 检查是否真正 running
    STATUS=$("$BRIDGE_BIN" status 2>/dev/null)
    if echo "$STATUS" | grep -q '"running": true'; then
        echo "ensure-bridge: connected successfully"
        exit 0
    fi

    echo "ensure-bridge: attempt $i failed, retrying..."
    "$BRIDGE_BIN" stop 2>/dev/null
    sleep 2
done

# 所有重试失败 — 警告但不阻塞 agent 启动
echo "ensure-bridge: WARNING - bridge not connected after $MAX_RETRIES attempts, agent will start without Coze"
exit 0
