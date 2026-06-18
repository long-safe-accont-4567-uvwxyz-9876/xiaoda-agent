#!/bin/bash
# =============================================================================
#  Nahida Agent — Auto-Update Script
#  检查 GitHub Release 最新版本，自动下载更新
# =============================================================================
set -euo pipefail

REPO="${GITHUB_REPO:-nahida-agent/nahida-agent}"
GITHUB_API="https://api.github.com/repos/${REPO}"
INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_FILE="${INSTALL_DIR}/.version"
AUTO_UPDATE_FLAG="${INSTALL_DIR}/.auto_update"

bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }

# 检查是否开启自动更新
if [ ! -f "$AUTO_UPDATE_FLAG" ]; then
    exit 0
fi

# 获取当前版本
CURRENT_VERSION=""
if [ -f "$VERSION_FILE" ]; then
    CURRENT_VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
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
    EXTRACT_CMD="tar xzf"
elif [ "$OS" = "Linux" ] && [ "$ARCH" = "aarch64" ]; then
    PLATFORM="linux-arm64"
    EXT="run"
    EXTRACT_CMD=""
else
    echo "  不支持的平台: ${OS}-${ARCH}，跳过自动更新"
    exit 0
fi

# 查找下载 URL
PATTERN="nahida-agent-${PLATFORM}-v${LATEST_VERSION}.${EXT}"
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

# 下载更新
TMP_DIR=$(mktemp -d)
FILENAME="$(basename "$DOWNLOAD_URL")"
echo "  下载中: ${FILENAME} ..."
curl -L --progress-bar --connect-timeout 10 --max-time 300 -o "${TMP_DIR}/${FILENAME}" "$DOWNLOAD_URL"

if [ ! -f "${TMP_DIR}/${FILENAME}" ]; then
    echo "  $(red "下载失败")"
    rm -rf "$TMP_DIR"
    exit 1
fi

echo "  下载完成，开始更新..."

# 备份当前版本
BACKUP_DIR="${INSTALL_DIR}/.backup_v${CURRENT_VERSION:-unknown}"
if [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
fi

# 停止运行中的实例
echo "  停止运行中的服务..."
pkill -f "nahida-agent" 2>/dev/null || true
sleep 1

# 备份关键文件（.env, config, credentials, data）
mkdir -p "$BACKUP_DIR"
for item in .env config credentials data; do
    if [ -e "${INSTALL_DIR}/${item}" ]; then
        cp -r "${INSTALL_DIR}/${item}" "$BACKUP_DIR/"
    fi
done

# 解压更新
if [ "$EXT" = "tar.gz" ]; then
    tar xzf "${TMP_DIR}/${FILENAME}" -C "${TMP_DIR}/extract"
    # 将新文件复制到安装目录
    cp -rf "${TMP_DIR}/extract/nahida-agent/"* "${INSTALL_DIR}/"
elif [ "$EXT" = "run" ]; then
    # .run 安装包需要交互式安装，跳过自动更新
    echo "  $(yellow "ARM64 版本需要手动安装更新")"
    echo "  下载文件: ${TMP_DIR}/${FILENAME}"
    rm -rf "$TMP_DIR"
    exit 0
fi

# 恢复用户配置
for item in .env config credentials data; do
    if [ -e "$BACKUP_DIR/$item" ]; then
        cp -rf "$BACKUP_DIR/$item" "${INSTALL_DIR}/"
    fi
done

# 更新版本号
echo "$LATEST_VERSION" > "$VERSION_FILE"

# 清理
rm -rf "$TMP_DIR" "$BACKUP_DIR"

echo ""
echo "  ╔═══════════════════════════════════════════════╗"
green "  ║  更新完成! v${LATEST_VERSION}                  ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo ""
