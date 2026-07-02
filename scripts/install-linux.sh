#!/usr/bin/env bash
set -euo pipefail

# ── 小达 Agent Linux 安装脚本 ──────────────────────────────
# 用法: curl -sL https://raw.githubusercontent.com/.../install-linux.sh | bash
# 或:   bash install-linux.sh

INSTALL_DIR="${INSTALL_DIR:-$HOME/.xiaoda-agent}"
SERVICE_NAME="xiaoda-agent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── 检查依赖 ──────────────────────────────────────────────
check_deps() {
    local missing=()
    for cmd in python3 pip3; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        error "缺少依赖: ${missing[*]}。请先安装 Python 3.11+"
    fi

    # 检查 Python 版本 >= 3.11
    local pyver
    pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    local major minor
    major=$(echo "$pyver" | cut -d. -f1)
    minor=$(echo "$pyver" | cut -d. -f2)
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
        error "需要 Python >= 3.11，当前: $pyver"
    fi
    info "Python $pyver"
}

# ── 解压安装 ──────────────────────────────────────────────
install_agent() {
    local tarball="$1"

    mkdir -p "$INSTALL_DIR"
    tar -xzf "$tarball" -C "$INSTALL_DIR" --strip-components=1
    info "解压到 $INSTALL_DIR"

    # 安装 Python 依赖
    if [ -f "$INSTALL_DIR/requirements.txt" ]; then
        pip3 install -r "$INSTALL_DIR/requirements.txt" --quiet 2>/dev/null || \
            pip3 install -r "$INSTALL_DIR/requirements.txt" 2>&1 | tail -5
        info "Python 依赖已安装"
    fi

    # 创建 .env（如果不存在）
    if [ ! -f "$INSTALL_DIR/.env" ]; then
        cat > "$INSTALL_DIR/.env" <<'ENVEOF'
# ── 小达 Agent 配置 ──
WEBUI_HOST=0.0.0.0
WEBUI_PORT=8080
# LLM_API_KEY=sk-your-key-here
# LLM_BASE_URL=https://api.openai.com/v1
ENVEOF
        info "已创建 .env 配置文件（请编辑填入 API Key）"
    fi
}

# ── 创建 systemd 服务 ─────────────────────────────────────
setup_service() {
    if [ ! -d /etc/systemd/system ]; then
        warn "未检测到 systemd，跳过服务创建。请手动运行: python3 $INSTALL_DIR/agent.py --web"
        return
    fi

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=小达 AI Agent (WebUI + QQ Bot)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/agent.py --web --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
    info "服务已创建并启动: $SERVICE_NAME"
}

# ── 主流程 ────────────────────────────────────────────────
main() {
    echo ""
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║    小达 Agent Linux 安装程序         ║"
    echo "  ╚══════════════════════════════════════╝"
    echo ""

    check_deps

    # 查找 tar.gz
    local tarball=""
    if [ -n "${1:-}" ] && [ -f "${1:-}" ]; then
        tarball="$1"
    else
        # 在当前目录查找
        tarball=$(ls xiaoda-agent-linux-x86_64-*.tar.gz 2>/dev/null | head -1)
    fi

    if [ -z "$tarball" ] || [ ! -f "$tarball" ]; then
        error "请指定 tar.gz 文件: bash install-linux.sh xiaoda-agent-linux-x86_64-vX.X.X.tar.gz"
    fi

    info "安装包: $tarball"
    install_agent "$tarball"
    setup_service

    echo ""
    info "安装完成！"
    echo ""
    echo "  访问地址: http://localhost:8080"
    echo "  配置文件: $INSTALL_DIR/.env"
    echo "  服务管理: sudo systemctl {start|stop|restart|status} $SERVICE_NAME"
    echo ""
}

main "$@"
