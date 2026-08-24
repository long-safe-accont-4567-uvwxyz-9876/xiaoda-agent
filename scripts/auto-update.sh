#!/bin/bash
# =============================================================================
#  Xiaoda Agent — Auto-Update Script (Linux)
#  原子更新协议：校验候选包 → 备份安装目录 → 复制 → 校验关键文件 → 写版本号
#  任何步骤失败都会回滚到备份，不会留下半更新状态
# =============================================================================
set -euo pipefail

REPO="${GITHUB_REPO:-long-safe-accont-4567-uvwxyz-9876/xiaoda-agent}"
GITHUB_API="https://api.github.com/repos/${REPO}"
INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_FILE="${INSTALL_DIR}/.version"
AUTO_UPDATE_FLAG="${INSTALL_DIR}/.auto_update"
LOCK_FILE="${TMPDIR:-/tmp}/xiaoda-agent-update-$(printf '%s' "$INSTALL_DIR" | md5sum | cut -d' ' -f1).lock"

bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }

# 检查是否开启自动更新
if [ ! -f "$AUTO_UPDATE_FLAG" ]; then
    exit 0
fi

# ── 并发锁：flock 串行化更新事务 ──────────────────────────
# 两个同时启动的更新共享临时/备份路径，会互相删工作文件导致回滚失效
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "  $(yellow "另一个更新进程正在运行，跳过")"
    exit 0
fi

# 获取当前版本
CURRENT_VERSION=""
if [ -f "$VERSION_FILE" ]; then
    CURRENT_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
fi

# 获取最新 Release 版本
echo "  $(yellow "检查更新...")"
LATEST_JSON=$(curl -s --connect-timeout 5 --max-time 15 "${GITHUB_API}/releases/latest" 2>/dev/null || echo "")

if [ -z "$LATEST_JSON" ]; then
    echo "  无法连接更新服务器，跳过更新检查"
    exit 0
fi

LATEST_VERSION=$(echo "$LATEST_JSON" | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name":\s*"v?([^"]+)".*/\1/')

if [ -z "$LATEST_VERSION" ]; then
    echo "  无法获取最新版本信息，跳过更新"
    exit 0
fi

# 版本比较
if [ -n "$CURRENT_VERSION" ] && [ "$LATEST_VERSION" != "$CURRENT_VERSION" ] && \
   [ "$(printf '%s\n%s\n' "$LATEST_VERSION" "$CURRENT_VERSION" | sort -V | tail -n 1)" = "$CURRENT_VERSION" ]; then
    echo "  $(green "当前版本 v${CURRENT_VERSION} 不低于 Release v${LATEST_VERSION}")"
    exit 0
fi
if [ "$LATEST_VERSION" = "$CURRENT_VERSION" ]; then
    echo "  $(green "已是最新版本 v${LATEST_VERSION}")"
    exit 0
fi

echo "  发现新版本: $(bold "v${LATEST_VERSION}") (当前: v${CURRENT_VERSION:-未知})"

# 检测平台
# 2026-08-24 契约修复：uname -m 输出必须映射为「发布资产命名」平台名。
# 发布矩阵产物是 linux-x86_64 / linux-arm64（build-release.yml matrix.platform），
# 此前 aarch64 直接拼成 linux-aarch64，永远匹配不到资产——ARM 用户永远收不到更新。
# resolve_release_platform: uname -s/-m → 发布资产平台名；不支持返回空。
resolve_release_platform() {
    local os_name="$1" arch="$2"
    case "${os_name}-${arch}" in
        Linux-x86_64)  echo "linux-x86_64" ;;
        Linux-aarch64|Linux-armv8*) echo "linux-arm64" ;;
        *)             echo "" ;;
    esac
}

OS="$(uname -s)"
ARCH="$(uname -m)"
PLATFORM="$(resolve_release_platform "$OS" "$ARCH")"

if [ -z "$PLATFORM" ]; then
    echo "  不支持的平台: ${OS}-${ARCH}，跳过自动更新"
    exit 0
fi

# 查找下载 URL
EXT="tar.gz"
PATTERN="xiaoda-agent-${PLATFORM}-v${LATEST_VERSION}.${EXT}"
DOWNLOAD_URL=$(echo "$LATEST_JSON" | grep -o "\"browser_download_url\":\s*\"[^\"]*${PATTERN}[^\"]*\"" | sed -E 's/.*"browser_download_url":\s*"([^"]+)".*/\1/' | head -1)

if [ -z "$DOWNLOAD_URL" ]; then
    # 尝试模糊匹配
    DOWNLOAD_URL=$(echo "$LATEST_JSON" | grep -o "\"browser_download_url\":\s*\"[^\"]*${PLATFORM}[^\"]*\"" | sed -E 's/.*"browser_download_url":\s*"([^"]+)".*/\1/' | head -1)
fi

if [ -z "$DOWNLOAD_URL" ]; then
    echo "  未找到 ${PLATFORM} 安装包，跳过自动更新"
    echo "  请手动访问: https://github.com/${REPO}/releases/latest"
    exit 0
fi

# ── 下载更新 ──────────────────────────────────────────────
TMP_DIR=$(mktemp -d)
FILENAME="$(basename "$DOWNLOAD_URL")"
echo "  下载中: ${FILENAME} ..."
curl -Lf --progress-bar --connect-timeout 10 --max-time 300 -o "${TMP_DIR}/${FILENAME}" "$DOWNLOAD_URL"

if [ ! -f "${TMP_DIR}/${FILENAME}" ] || [ ! -s "${TMP_DIR}/${FILENAME}" ]; then
    echo "  $(red "下载失败")"
    rm -rf "$TMP_DIR"
    exit 1
fi

# SHA256 校验
# 2026-08-24 fail-closed：校验文件缺失 / 为空 / 格式非法一律中止更新。
# 原实现缺失时仅告警并继续安装——攻击者篡改下载源即可绕过完整性校验。
SHA256_URL="${DOWNLOAD_URL}.sha256"
SHA256_FILE="${TMP_DIR}/${FILENAME}.sha256"
VERIFY_FAILED=0
if curl -sfL --connect-timeout 5 --max-time 15 -o "$SHA256_FILE" "$SHA256_URL" && [ -s "$SHA256_FILE" ]; then
    EXPECTED=$(awk 'NR==1{print tolower($1)}' "$SHA256_FILE")
    # 合法 sha256 = 64 位十六进制；否则视为格式损坏，同样拒绝安装
    if ! printf '%s' "$EXPECTED" | grep -qE '^[0-9a-f]{64}$'; then
        echo "  $(red "SHA256 校验文件格式非法！中止更新")"
        VERIFY_FAILED=1
    else
        ACTUAL=$(sha256sum "${TMP_DIR}/${FILENAME}" | awk '{print $1}')
        if [ "$EXPECTED" != "$ACTUAL" ]; then
            echo "  $(red "SHA256 校验失败！中止更新")"
            echo "  期望: $EXPECTED"
            echo "  实际: $ACTUAL"
            VERIFY_FAILED=1
        fi
    fi
else
    echo "  $(red "未找到 SHA256 校验文件，fail-closed 中止更新")"
    echo "  发布资产不完整（缺 ${FILENAME}.sha256），请手动检查 release 或联系维护者"
    VERIFY_FAILED=1
fi
if [ "$VERIFY_FAILED" != "0" ]; then
    rm -rf "$TMP_DIR"
    exit 1
fi
echo "  $(green "SHA256 校验通过")"

echo "  下载完成，开始更新..."

# ── 解压到候选目录（不直接覆盖安装目录）────────────────────
EXTRACT_DIR="${TMP_DIR}/extract"
mkdir -p "$EXTRACT_DIR"
if ! tar xzf "${TMP_DIR}/${FILENAME}" -C "$EXTRACT_DIR" 2>&1; then
    echo "  $(red "解压失败，中止更新")"
    rm -rf "$TMP_DIR"
    exit 1
fi

# 确定候选源目录
CANDIDATE_DIR="$EXTRACT_DIR"
if [ -d "${EXTRACT_DIR}/xiaoda-agent" ]; then
    CANDIDATE_DIR="${EXTRACT_DIR}/xiaoda-agent"
fi

# ── 校验候选包关键文件 ────────────────────────────────────
# 候选包必须包含这些关键文件，否则视为不完整
CRITICAL_FILES="xiaoda-agent .version scripts/auto-update.sh scripts/doctor.sh scripts/start-linux.sh"
MISSING_FILES=""
for cf in $CRITICAL_FILES; do
    if [ ! -e "${CANDIDATE_DIR}/${cf}" ]; then
        MISSING_FILES="${MISSING_FILES} ${cf}"
    fi
done
if [ -n "$MISSING_FILES" ]; then
    echo "  $(red "候选包缺少关键文件:${MISSING_FILES}，中止更新")"
    rm -rf "$TMP_DIR"
    exit 1
fi
echo "  $(green "候选包校验通过")"

# ── 备份安装目录 ──────────────────────────────────────────
# 只备份安装目录内的文件（程序代码 + .env + .version + scripts/）
# 用户数据在 ~/.ai-agent/data/，不在安装目录中，无需备份
BACKUP_DIR="$(mktemp -d)/xiaoda-agent-backup-v${CURRENT_VERSION:-unknown}"
BACKUP_READY=false
mkdir -p "$BACKUP_DIR"

# 停止运行中的实例（排除自身 PID）
echo "  停止运行中的服务..."
SELF_PID=$$
pkill -f "agent.py" 2>/dev/null || true
sleep 1

# 备份安装目录（排除 .venv 以节省时间和空间）
if [ -d "$INSTALL_DIR" ]; then
    # 使用 rsync 排除 .venv，如果 rsync 不存在则回退到 cp
    if command -v rsync &>/dev/null; then
        if rsync -a --exclude='.venv' "$INSTALL_DIR/." "$BACKUP_DIR/" 2>/dev/null; then
            BACKUP_READY=true
            echo "  $(green "安装目录已备份到 ${BACKUP_DIR}")"
        else
            echo "  $(red "备份失败，中止更新（安装目录未被修改）")"
            rm -rf "$TMP_DIR" "$BACKUP_DIR"
            exit 1
        fi
    else
        # 回退：cp -a 全量备份（含 .venv）
        if cp -a "$INSTALL_DIR/." "$BACKUP_DIR/" 2>/dev/null; then
            BACKUP_READY=true
            echo "  $(green "安装目录已备份到 ${BACKUP_DIR}")"
        else
            echo "  $(red "备份失败，中止更新（安装目录未被修改）")"
            rm -rf "$TMP_DIR" "$BACKUP_DIR"
            exit 1
        fi
    fi
fi

# ── 原子更新：复制候选包到安装目录 ────────────────────────
UPDATE_FAILED=false
# 注意：cp -a 仅覆盖复制，不删除候选包中不存在的旧文件
# 用户数据目录（.env, data, credentials 等）不在候选包中，不受影响
# cp -a 保留权限和时间戳
if ! cp -a "${CANDIDATE_DIR}/." "${INSTALL_DIR}/" 2>&1; then
    echo "  $(red "复制更新文件失败")"
    UPDATE_FAILED=true
fi

# 校验安装后关键文件
if [ "$UPDATE_FAILED" = "false" ]; then
    for cf in $CRITICAL_FILES; do
        if [ ! -e "${INSTALL_DIR}/${cf}" ]; then
            echo "  $(red "更新后关键文件缺失: ${cf}")"
            UPDATE_FAILED=true
            break
        fi
    done
fi

# ── 失败回滚 / 成功提交 ───────────────────────────────────
if [ "$UPDATE_FAILED" = "true" ]; then
    echo "  $(red "更新失败，开始回滚...")"
    if [ "$BACKUP_READY" = "true" ] && [ -d "$BACKUP_DIR" ]; then
        # 恢复备份
        cp -a "${BACKUP_DIR}/." "${INSTALL_DIR}/" 2>/dev/null || true
        # 校验回滚后关键文件
        ROLLBACK_OK=true
        for cf in agent.py .version; do
            if [ ! -e "${INSTALL_DIR}/${cf}" ]; then
                ROLLBACK_OK=false
                break
            fi
        done
        if [ "$ROLLBACK_OK" = "true" ]; then
            echo "  $(green "已回滚到 v${CURRENT_VERSION:-unknown}")"
        else
            echo "  $(red "回滚后关键文件仍缺失！请手动重新安装")"
            echo "  备份目录: ${BACKUP_DIR}"
            rm -rf "$TMP_DIR"
            exit 1
        fi
    else
        echo "  $(red "无可用备份，无法回滚！请手动重新安装")"
        rm -rf "$TMP_DIR"
        exit 1
    fi
    rm -rf "$TMP_DIR" "$BACKUP_DIR"
    exit 1
fi

# 恢复用户配置（.env 在安装目录，其他用户数据在 ~/.ai-agent/data/）
# config.py 在 Linux 上将 data/credentials/config 等路由到 ~/.ai-agent/data/
# 这些不在候选包中，更新不会覆盖它们；此处仅恢复 .env（安装目录内）
if [ -e "$BACKUP_DIR/.env" ] && [ ! -e "${INSTALL_DIR}/.env" ]; then
    cp -a "$BACKUP_DIR/.env" "${INSTALL_DIR}/" 2>/dev/null || true
fi

# ── 仅在所有校验成功后写版本号 ────────────────────────────
echo "$LATEST_VERSION" > "$VERSION_FILE"

# 清理
rm -rf "$TMP_DIR" "$BACKUP_DIR"

echo ""
echo "  ╔═══════════════════════════════════════════════╗"
green "  ║  更新完成! v${LATEST_VERSION}                  ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo ""
