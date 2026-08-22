#!/bin/bash
# =============================================================================
#  Xiaoda Agent — WebUI systemd 服务安装（源码直跑部署专用）
#
# 背景：tarball 安装（install-linux.sh）已为整机创建 xiaoda-agent.service
# （含 WebUI 启动）。本脚本面向「源码直跑」部署（git clone 后手动
# nohup 启动 agent.py --web 的场景），把 WebUI 纳入 systemd 托管：
# 崩溃自动重启 + 开机自启，替代手工 nohup。
#
# 用法：bash scripts/install-webui-service.sh [--port 8080]
#       默认 127.0.0.1:8080（与 CLAUDE.md 一致）；--host 0.0.0.0 需自行评估暴露面
# 卸载：sudo systemctl disable --now xiaoda-webui && sudo rm /etc/systemd/system/xiaoda-webui.service
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="xiaoda-webui"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

WEBUI_HOST="127.0.0.1"
WEBUI_PORT="${WEBUI_PORT:-8080}"
while [ $# -gt 0 ]; do
    case "$1" in
        --port) WEBUI_PORT="$2"; shift 2 ;;
        --host) WEBUI_HOST="$2"; shift 2 ;;
        -h|--help) sed -n '1,16p' "$0" | grep -E '^#|^$' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1（--help 查看用法）"; exit 2 ;;
    esac
done

# .env 可覆盖 WEBUI_PORT（与项目 load_dotenv 行为一致，但脚本参数优先）
if [ -f "$INSTALL_DIR/.env" ]; then
    ENV_PORT="$(grep -E '^WEBUI_PORT=' "$INSTALL_DIR/.env" | tail -1 | cut -d= -f2 || true)"
    [ -n "${ENV_PORT:-}" ] && WEBUI_PORT="$ENV_PORT"
fi

if [ ! -f "$INSTALL_DIR/scripts/start-linux.sh" ]; then
    echo "错误：未找到 $INSTALL_DIR/scripts/start-linux.sh（确认在项目根运行）" >&2
    exit 1
fi
if [ ! -f "$INSTALL_DIR/agent.py" ]; then
    echo "错误：未找到 $INSTALL_DIR/agent.py（本脚本只支持源码直跑部署）" >&2
    exit 1
fi

# 迁移提示：现有手动 nohup 进程与本服务端口冲突，先由用户停掉
if pgrep -f "agent.py.*--web" > /dev/null 2>&1; then
    echo "提示：检测到手动启动的 WebUI 进程（agent.py --web），"
    echo "      服务启用后请手动停止它，以免端口冲突："
    echo "      pkill -f 'agent.py.*--web'"
fi

# 以当前调用用户运行服务。不能用 systemd 说明符 %u/%h：system 管理器下
# %u 恒解析为 "root"、%h 解析为 /root（man systemd.unit SPECIFIERS，实证于
# systemd 252），会导致 WebUI 以 root 运行——故安装时把真实用户写进 unit。
RUN_USER="$(id -un)"
RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
if [ -z "$RUN_USER_HOME" ]; then
    RUN_USER_HOME="$(eval echo "~${RUN_USER}")"
fi

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Xiaoda Agent Web UI（源码直跑托管）
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=$INSTALL_DIR
# start-linux.sh 自带看门狗崩溃重启；systemd 层负责开机自启与兜底
ExecStart=$INSTALL_DIR/scripts/start-linux.sh --web --host $WEBUI_HOST --port $WEBUI_PORT
Restart=on-failure
RestartSec=5
# 防止环境变量依赖用户登录会话
Environment=HOME=${RUN_USER_HOME}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "✓ 服务已创建：$SERVICE_NAME (host=$WEBUI_HOST port=$WEBUI_PORT)"
echo "  sudo systemctl start $SERVICE_NAME   # 启动"
echo "  sudo systemctl status $SERVICE_NAME  # 状态"
echo "  journalctl -u $SERVICE_NAME -f       # 日志"