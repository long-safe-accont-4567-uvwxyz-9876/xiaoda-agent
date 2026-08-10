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
    for cmd in tar; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        error "缺少依赖: ${missing[*]}"
    fi
    info "安装依赖检查通过"
}

# ── 解压安装 ──────────────────────────────────────────────
install_agent() {
    local tarball="$1"

    mkdir -p "$INSTALL_DIR"
    tar -xzf "$tarball" -C "$INSTALL_DIR" --strip-components=1
    info "解压到 $INSTALL_DIR"

    if [ ! -x "$INSTALL_DIR/xiaoda-agent" ]; then
        error "安装包缺少可执行文件: $INSTALL_DIR/xiaoda-agent"
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

    # 容错：chmod/ln 失败仅告警，绝不中止安装（set -euo pipefail 下需显式守护）
    if ! chmod +x "$cli" 2>/dev/null; then
        warn "无法设置 $cli 可执行权限，请手动运行: chmod +x $cli"
        return
    fi

    # 已注册到 /usr/local/bin：软链成功即视为完成
    if [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
        if ln -sf "$cli" /usr/local/bin/xiaoda 2>/dev/null; then
            info "已创建命令: /usr/local/bin/xiaoda"
            return
        fi
        warn "写入 /usr/local/bin 失败，尝试其他方式"
    fi

    # 需要 sudo 时写入 /usr/local/bin（sudo -n true 不保证 sudo ln 必成功，故对 ln 单独容错）
    if [ -d /usr/local/bin ] && sudo -n true 2>/dev/null; then
        if sudo -n ln -sf "$cli" /usr/local/bin/xiaoda 2>/dev/null; then
            info "已创建命令: /usr/local/bin/xiaoda"
            return
        fi
        warn "sudo 写入 /usr/local/bin 失败，尝试其他方式"
    fi

    # 兜底：把安装目录的 scripts 加入 ~/.bashrc 的 PATH
    local bashrc="$HOME/.bashrc"
    local line="export PATH=\"$INSTALL_DIR/scripts:\$PATH\""
    if grep -qF "$INSTALL_DIR/scripts" "$bashrc" 2>/dev/null; then
        info "PATH 已包含 $INSTALL_DIR/scripts"
        return
    fi
    if [ ! -f "$bashrc" ]; then
        warn "未找到 ~/.bashrc，无法自动配置 PATH"
        warn "请手动执行: export PATH=\"$INSTALL_DIR/scripts:\$PATH\""
        return
    fi
    if echo "$line" >> "$bashrc" 2>/dev/null; then
        info "已将 $INSTALL_DIR/scripts 加入 ~/.bashrc 的 PATH"
    else
        warn "写入 ~/.bashrc 失败，请手动执行: export PATH=\"$INSTALL_DIR/scripts:\$PATH\""
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
