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
LOCK_FILE="/tmp/xiaoda-agent-update.lock"

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
if [ "$LATEST_VERSION" = "$CURRENT_VERSION" ]; then
    echo "  $(green "已是最新版本 v${LATEST_VERSION}")"
    exit 0
fi

echo "  发现新版本: $(bold "v${LATEST_VERSION}") (当前: v${CURRENT_VERSION:-未知})"

# 检测平台
OS="$(uname -s)"
ARCH="$(uname -m)"

if [ "$OS" = "Linux" ] && [ "$ARCH" = "x86_64" ]; then
    PLATFORM="linux-x86_64"
    EXT="tar.gz"
elif [ "$OS" = "Linux" ] && [ "$ARCH" = "aarch64" ]; then
    PLATFORM="linux-aarch64"
    EXT="tar.gz"
else
    echo "  不支持的平台: ${OS}-${ARCH}，跳过自动更新"
    exit 0
fi

# 查找下载 URL
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
SHA256_URL="${DOWNLOAD_URL}.sha256"
SHA256_FILE="${TMP_DIR}/${FILENAME}.sha256"
if curl -sL --connect-timeout 5 --max-time 15 -o "$SHA256_FILE" "$SHA256_URL" 2>/dev/null && [ -s "$SHA256_FILE" ]; then
    EXPECTED=$(awk '{print $1}' "$SHA256_FILE")
    ACTUAL=$(sha256sum "${TMP_DIR}/${FILENAME}" | awk '{print $1}')
    if [ "$EXPECTED" != "$ACTUAL" ]; then
        echo "  $(red "SHA256 校验失败！中止更新")"
        echo "  期望: $EXPECTED"
        echo "  实际: $ACTUAL"
        rm -rf "$TMP_DIR"
        exit 1
    fi
    echo "  $(green "SHA256 校验通过")"
else
    echo "  $(yellow "警告: 未找到 SHA256 校验文件，跳过校验")"
fi

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
CRITICAL_FILES="agent.py .version scripts/auto-update.sh scripts/doctor.sh scripts/start-linux.sh"
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
BACKUP_DIR="$(mktemp -d)/xiaoda-agent-backup-v${CURRENT_VERSION:-unknown}"
BACKUP_READY=false
mkdir -p "$BACKUP_DIR"

# 停止运行中的实例（排除自身 PID）
echo "  停止运行中的服务..."
SELF_PID=$$
pkill -f "agent.py" 2>/dev/null || true
sleep 1

# 备份整个安装目录
if [ -d "$INSTALL_DIR" ]; then
    if cp -a "$INSTALL_DIR/." "$BACKUP_DIR/" 2>/dev/null; then
        BACKUP_READY=true
        echo "  $(green "安装目录已备份到 ${BACKUP_DIR}")"
    else
        echo "  $(red "备份失败，中止更新（安装目录未被修改）")"
        rm -rf "$TMP_DIR" "$BACKUP_DIR"
        exit 1
    fi
fi

# ── 原子更新：复制候选包到安装目录 ────────────────────────
UPDATE_FAILED=false
# 使用 rsync 风格的覆盖：先复制新文件，再删除旧文件
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

# 恢复用户配置（.env, data, credentials 等不在候选包中的文件）
for item in .env config credentials data stickers xiaoli-stickers agent-stickers media voice_refs files memory_state plugins workspace; do
    if [ -e "$BACKUP_DIR/$item" ] && [ ! -e "${INSTALL_DIR}/${item}" ]; then
        cp -a "$BACKUP_DIR/$item" "${INSTALL_DIR}/" 2>/dev/null || true
    fi
done

# ── 仅在所有校验成功后写版本号 ────────────────────────────
echo "$LATEST_VERSION" > "$VERSION_FILE"

# 清理
rm -rf "$TMP_DIR" "$BACKUP_DIR"

echo ""
echo "  ╔═══════════════════════════════════════════════╗"
green "  ║  更新完成! v${LATEST_VERSION}                  ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo ""
