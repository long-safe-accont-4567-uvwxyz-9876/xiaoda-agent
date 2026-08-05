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

    # 创建 Python 虚拟环境
    if [ ! -d "$INSTALL_DIR/.venv" ]; then
        python3 -m venv "$INSTALL_DIR/.venv"
        info "已创建 Python 虚拟环境"
    fi

    # 安装 Python 依赖到 venv
    if [ -f "$INSTALL_DIR/requirements.txt" ]; then
        "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet 2>/dev/null || \
            "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" 2>&1 | tail -5
        info "Python 依赖已安装"
    fi

    # 创建 .env（如果不存在）
    if [ ! -f "$INSTALL_DIR/.env" ]; then
        cat > "$INSTALL_DIR/.env" <<'ENVEOF'
# ── 小达 Agent 配置 ──
WEBUI_HOST=0.0.0.0
WEBUI_PORT=8082
# LLM_API_KEY=sk-your-key-here
# LLM_BASE_URL=https://api.openai.com/v1
ENVEOF
        info "已创建 .env 配置文件（请编辑填入 API Key）"
    fi

    # 创建用户数据目录（与 config.py 中 _resolve_data_path 的目录结构对齐）
    mkdir -p "$HOME/.ai-agent/data/db" \
             "$HOME/.ai-agent/data/logs" \
             "$HOME/.ai-agent/data/credentials" \
             "$HOME/.ai-agent/data/config" \
             "$HOME/.ai-agent/data/config/workspace" \
             "$HOME/.ai-agent/data/config/agents" \
             "$HOME/.ai-agent/data/stickers" \
             "$HOME/.ai-agent/data/xiaoli-stickers" \
             "$HOME/.ai-agent/data/agent-stickers" \
             "$HOME/.ai-agent/data/media" \
             "$HOME/.ai-agent/data/files" \
             "$HOME/.ai-agent/data/voice_refs" \
             "$HOME/.ai-agent/data/memory_state" \
             "$HOME/.ai-agent/data/plugins" \
             "$HOME/.ai-agent/data/workspace"
    info "用户数据目录已创建"
}

# ── 配置 xiaoda CLI 命令到 PATH ───────────────────────────
setup_cli_command() {
    local cli="$INSTALL_DIR/scripts/xiaoda"

    # 安装包可能未包含 scripts/xiaoda（旧包或未重新打包），此时不应中止安装，
    # 仅提示用户手动处理即可。
    if [ ! -f "$cli" ]; then
        warn "未找到 CLI 入口脚本: $cli（安装包可能未打包 scripts/）"
        warn "请先重新打包安装包，或手动运行: python $INSTALL_DIR/agent.py --cli"
        return
    fi
    chmod +x "$cli"

    # 优先尝试 /usr/local/bin（免改用户 shell 配置，全局可用）
    if [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
        ln -sf "$cli" /usr/local/bin/xiaoda
        info "已创建命令: /usr/local/bin/xiaoda"
        return
    fi

    # 需要 sudo 时写入 /usr/local/bin
    if [ -d /usr/local/bin ] && sudo -n true 2>/dev/null; then
        sudo ln -sf "$cli" /usr/local/bin/xiaoda
        info "已创建命令: /usr/local/bin/xiaoda"
        return
    fi

    # 兜底：把安装目录的 scripts 加入 ~/.bashrc 的 PATH
    local bashrc="$HOME/.bashrc"
    local line="export PATH=\"$INSTALL_DIR/scripts:\$PATH\""
    if [ -f "$bashrc" ] && ! grep -qF "$INSTALL_DIR/scripts" "$bashrc"; then
        echo "$line" >> "$bashrc"
        info "已将 $INSTALL_DIR/scripts 加入 ~/.bashrc 的 PATH"
    else
        warn "未自动配置 PATH，请手动执行: export PATH=\"$INSTALL_DIR/scripts:\$PATH\""
        warn "或: sudo ln -sf $cli /usr/local/bin/xiaoda"
    fi
}

# ── 创建 systemd 服务 ─────────────────────────────────────
setup_service() {
    if [ ! -d /etc/systemd/system ]; then
        warn "未检测到 systemd，跳过服务创建。请手动运行: bash $INSTALL_DIR/scripts/start-linux.sh --web"
        return
    fi

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=小达 AI Agent (WebUI + QQ Bot)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/scripts/start-linux.sh --web --host 0.0.0.0 --port \${WEBUI_PORT}
Restart=on-failure
RestartSec=5
# 看门狗达到 MAX_RESTARTS 后 exit 0 停止重启，systemd 不应对 exit 0 重启
RestartPreventExitStatus=0
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$INSTALL_DIR/.env

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

    # 查找 tar.gz — 支持 .run 自解压和直接指定两种方式
    local tarball=""
    if [ -n "${1:-}" ] && [ -f "${1:-}" ]; then
        tarball="$1"
    else
        # 检查是否为 .run 自解压（内嵌 __ARCHIVE__ 标记）
        if grep -q '__ARCHIVE__' "$0" 2>/dev/null; then
            local archive_line
            archive_line=$(grep -n '__ARCHIVE__' "$0" | head -1 | cut -d: -f1)
            local tmp_tarball=$(mktemp /tmp/xiaoda-agent-XXXXXX.tar.gz)
            tail -n +$((archive_line + 1)) "$0" > "$tmp_tarball"
            tarball="$tmp_tarball"
            info "检测到自解压安装包"
        else
            # 在当前目录查找
            tarball=$(ls xiaoda-agent-linux-x86_64-*.tar.gz 2>/dev/null | head -1)
        fi
    fi

    if [ -z "$tarball" ] || [ ! -f "$tarball" ]; then
        error "请指定 tar.gz 文件: bash install-linux.sh xiaoda-agent-linux-x86_64-vX.X.X.tar.gz"
    fi

    info "安装包: $tarball"
    install_agent "$tarball"
    setup_cli_command
    setup_service

    echo ""
    info "安装完成！"
    echo ""
    echo "  访问地址: http://localhost:8082"
    echo "  配置文件: $INSTALL_DIR/.env"
    echo "  服务管理: sudo systemctl {start|stop|restart|status} $SERVICE_NAME"
    echo "  CLI 命令: xiaoda        （若新终端仍提示 command not found，先执行 source ~/.bashrc）"
    echo "  手动启动: bash $INSTALL_DIR/scripts/start-linux.sh --web"
    echo "  自检工具: bash $INSTALL_DIR/scripts/doctor.sh"
    echo "  启用自动更新: touch $INSTALL_DIR/.auto_update"
    echo ""
}

main "$@"