#!/usr/bin/env bash
set -euo pipefail

# ── 小达 Agent Linux 安装脚本 ──────────────────────────────
# 用法: curl -sL https://raw.githubusercontent.com/.../install-linux.sh | bash
# 或:   bash install-linux.sh

# 安装目录：默认取调用 shell 的 $HOME。sudo 提权安装时 $HOME 是 root 的
# home，之后会在 resolve_service_user 中按服务用户 home 重算（仅当用户
# 未显式指定 INSTALL_DIR 时）；显式指定则原样尊重。
_INSTALL_DIR_EXPLICIT=0
if [ -n "${INSTALL_DIR:-}" ]; then
    _INSTALL_DIR_EXPLICIT=1
else
    INSTALL_DIR="$HOME/.xiaoda-agent"
fi
SERVICE_NAME="xiaoda-agent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# 服务运行用户（由 resolve_service_user 解析后写死进 unit，避免服务以 root 运行）
SERVICE_USER=""
SERVICE_GROUP=""
SERVICE_HOME=""
# 用户数据目录（与 config.py 的 ~/.ai-agent 对齐；以服务用户 home 为准）
DATA_DIR=""
FORCE=0

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
WEBUI_HOST=127.0.0.1
WEBUI_PORT=8082
# LLM_API_KEY=sk-your-key-here
# LLM_BASE_URL=https://api.openai.com/v1
ENVEOF
        info "已创建 .env 配置文件（请编辑填入 API Key）"
    fi

    # 创建用户数据目录（与 config.py 中 _resolve_data_path 的目录结构对齐）
    # 注意：以服务运行用户的 home 为准（root 执行时 $HOME=/root 会导致数据写错位置）
    mkdir -p "$DATA_DIR/data/db" \
             "$DATA_DIR/data/logs" \
             "$DATA_DIR/data/credentials" \
             "$DATA_DIR/data/config" \
             "$DATA_DIR/data/config/workspace" \
             "$DATA_DIR/data/config/agents" \
             "$DATA_DIR/data/stickers" \
             "$DATA_DIR/data/xiaoli-stickers" \
             "$DATA_DIR/data/agent-stickers" \
             "$DATA_DIR/data/media" \
             "$DATA_DIR/data/files" \
             "$DATA_DIR/data/voice_refs" \
             "$DATA_DIR/data/memory_state" \
             "$DATA_DIR/data/plugins" \
             "$DATA_DIR/data/workspace"
    if [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != "root" ]; then
        chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR" 2>/dev/null || \
            warn "无法将 $DATA_DIR 归属到 $SERVICE_USER，服务可能无写入权限"
        # 安装目录同样归属服务用户（修复：sudo 安装时目录属 root，服务用户
        # 既读不了 frozen 可执行文件，自动更新器也无权写安装目录）
        chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR" 2>/dev/null || \
            warn "无法将 $INSTALL_DIR 归属到 $SERVICE_USER，服务可能无读取权限"
    fi
    info "用户数据目录已创建 ($DATA_DIR)"
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

# ── 解析服务运行用户（非 root）────────────────────────────
# systemd unit 必须写死 User=，否则 WebUI 的 pty.fork 终端等能力会以 root 运行。
resolve_service_user() {
    local candidate=""
    # 优先 SUDO_USER（sudo/su 提权安装时还原真实用户），其次 logname，再退到当前用户
    if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
        candidate="${SUDO_USER}"
    elif command -v logname &>/dev/null && [ -n "$(logname 2>/dev/null)" ] && [ "$(logname 2>/dev/null)" != "root" ]; then
        candidate="$(logname 2>/dev/null)"
    elif [ "$(id -un)" != "root" ]; then
        candidate="$(id -un)"
    fi

    if [ -z "$candidate" ]; then
        # 纯 root 环境（如容器/CI）：无可用非 root 用户
        if [ "$FORCE" -eq 1 ]; then
            warn "--force 已指定：服务将以 root 运行（不推荐）"
            SERVICE_USER="root"
            SERVICE_GROUP="root"
            SERVICE_HOME="${INSTALL_DIR}"
        else
            error "当前以 root 身份安装且未解析到真实用户，服务将以 root 运行（安全隐患）。请用普通用户执行本脚本，或显式传入 --force 以确认继续。"
        fi
    else
        # 校验用户与组真实存在
        if ! id "$candidate" &>/dev/null; then
            error "解析到的用户 $candidate 不存在，无法配置服务"
        fi
        SERVICE_USER="$candidate"
        SERVICE_GROUP="$(id -gn "$candidate")"
        SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
        if [ -z "$SERVICE_HOME" ]; then
            error "无法确定 $SERVICE_USER 的 home 目录"
        fi
        info "服务将以 ${SERVICE_USER}(${SERVICE_GROUP}) 运行，home=${SERVICE_HOME}"
    fi

    DATA_DIR="$SERVICE_HOME/.ai-agent"

    if [ "$SERVICE_USER" = "root" ] || [ -z "$SERVICE_USER" ]; then
        DATA_DIR="${HOME}/.ai-agent"
    fi

    # 修复（2026-08-30）：sudo 提权安装时调用 shell 的 $HOME 是 root 的 home，
    # 顶部默认的 INSTALL_DIR 会落在 /root/.xiaoda-agent，而服务以真实用户运行
    # ——WorkingDirectory/ExecStart 指向 root 私有目录，服务无法启动。
    # 与数据目录同一解析时机：默认值取自调用 HOME 且服务用户非 root 时，
    # 重算到服务用户 home 下；用户显式指定的 INSTALL_DIR 原样尊重。
    # （--force 的纯 root 环境 SERVICE_USER=root，此处不重算，保持原路径。）
    if [ "$_INSTALL_DIR_EXPLICIT" -eq 0 ] && [ "$SERVICE_USER" != "root" ] && [ -n "$SERVICE_HOME" ] \
        && [ "$INSTALL_DIR" = "${HOME}/.xiaoda-agent" ]; then
        INSTALL_DIR="$SERVICE_HOME/.xiaoda-agent"
        info "安装目录按服务用户 home 重算: $INSTALL_DIR"
    fi
}

# ── 创建 systemd 服务 ─────────────────────────────────────
setup_service() {
    if [ ! -d /etc/systemd/system ]; then
        warn "未检测到 systemd，跳过服务创建。请手动运行: bash $INSTALL_DIR/scripts/start-linux.sh --web"
        return
    fi

    resolve_service_user

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=小达 AI Agent (WebUI + QQ Bot)
After=network.target

[Service]
Type=simple
# 以真实用户运行：WebUI 带 pty.fork 终端，root 运行等于交出整台机器
User=$SERVICE_USER
Group=$SERVICE_GROUP
Environment=HOME=$SERVICE_HOME
# 路径一律加引号：安装目录可能含空格，systemd 按词拆分会拿到坏路径
WorkingDirectory="$INSTALL_DIR"
ExecStart="$INSTALL_DIR/scripts/start-linux.sh" --web --host 127.0.0.1 --port \${WEBUI_PORT}
Restart=on-failure
RestartSec=5
# 看门狗达到 MAX_RESTARTS 后 exit 0 停止重启，systemd 不应对 exit 0 重启
RestartPreventExitStatus=0
Environment=PYTHONUNBUFFERED=1
EnvironmentFile="$INSTALL_DIR/.env"
# ── 沙箱加固：限制 root 提权 / 文件系统 / tmp 写入 ──
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
ProtectHome=read-only
# 可写白名单：用户数据目录 + 安装目录（后者供自动更新器写入；
# ProtectHome=read-only 下若只放行数据目录，更新器永远装不上新版）
ReadWritePaths="$DATA_DIR" "$INSTALL_DIR"

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
    info "服务已创建并启动: $SERVICE_NAME"
}

# ── 主流程 ────────────────────────────────────────────────
usage() {
    echo "用法: bash install-linux.sh [--force] [xiaoda-agent-linux-x86_64-vX.X.X.tar.gz]"
    echo "  --force   root 环境且无可用普通用户时，强制以 root 安装服务（不推荐）"
}

main() {
    echo ""
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║    小达 Agent Linux 安装程序         ║"
    echo "  ╚══════════════════════════════════════╝"
    echo ""

    # 参数解析：--force（root 运行服务的显式确认）+ 可选 tarball 路径
    local args=()
    local arg
    for arg in "$@"; do
        case "$arg" in
            --force|-f) FORCE=1 ;;
            -h|--help)  usage; return 0 ;;
            *)          args+=("$arg") ;;
        esac
    done

    check_deps

    # 查找 tar.gz — 支持 .run 自解压和直接指定两种方式
    local tarball=""
    if [ ${#args[@]} -gt 0 ] && [ -n "${args[0]}" ] && [ -f "${args[0]}" ]; then
        tarball="${args[0]}"
    else
        # 检查是否为 .run 自解压（内嵌 __ARCHIVE__ 标记）
        # 2026-08-24 修复：必须只匹配整行 `__ARCHIVE__` 且取「最后一次」命中。
        # 原实现 grep -n '__ARCHIVE__' | head -1 是非锚定 + 首个命中——脚本头部
        # 注释里出现的 __ARCHIVE__ 字样会被误当 marker，tail 从注释处开始输出
        # 纯文本，tar 解压必坏。打包端在拼接 payload 前追加独立一行 __ARCHIVE__，
        # 因此真实 marker 永远是最后一个锚定命中。
        if grep -q '^__ARCHIVE__$' "$0" 2>/dev/null; then
            local archive_line
            archive_line=$(grep -n '^__ARCHIVE__$' "$0" | tail -1 | cut -d: -f1)
            local tmp_tarball
            tmp_tarball=$(mktemp /tmp/xiaoda-agent-XXXXXX.tar.gz)
            # trap 清理临时 tar 包：成功/失败/中断（EXIT 对 INT/TERM 同样触发）
            # 都要删除，避免几十 MB 的 payload 泄漏在 /tmp。
            # 捕获期即展开路径（双引号）——EXIT 触发时 local 变量可能已出栈。
            trap "rm -f '${tmp_tarball}'" EXIT
            tail -n +$((archive_line + 1)) "$0" > "$tmp_tarball"
            tarball="$tmp_tarball"
            info "检测到自解压安装包"
        else
            # 在当前目录查找（x86_64 与 arm64 命名均支持，与发布资产命名对齐）；
            # ls 无命中经 pipefail 会让赋值非零触发 set -e 退出，|| true 保住
            # 下方"请指定 tar.gz 文件"的友好报错路径
            tarball=$(ls xiaoda-agent-linux-x86_64-*.tar.gz xiaoda-agent-linux-arm64-*.tar.gz 2>/dev/null | head -1 || true)
        fi
    fi

    if [ -z "$tarball" ] || [ ! -f "$tarball" ]; then
        error "请指定 tar.gz 文件: bash install-linux.sh xiaoda-agent-linux-x86_64-vX.X.X.tar.gz"
    fi

    info "安装包: $tarball"
    resolve_service_user
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
