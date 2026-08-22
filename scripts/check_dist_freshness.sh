#!/bin/bash
# =============================================================================
# 前端构建物新鲜度校验 —— 防"改 src 忘 build"导致服务旧 UI（同类风险报告 #9）
#
# 背景：web/dist 已提交进 git 并由运行中的 FastAPI 直接托管（CLAUDE.md），
#       "改前端后 npm run build"是纯人工步骤，遗忘即静默服务过期产物。
#
# 原理：Vite 以内容哈希命名产物，源码不变 → 完整重建逐字节不变。
#       因此重建后 git status 出现任何 web/dist 差异 ⇒ 已提交的 dist
#       落后于前端源码 ⇒ 校验失败。
#
# 用法：
#   scripts/check_dist_freshness.sh                  # 含依赖安装（约 2 分钟）
#   SKIP_NPM_CI=1 scripts/check_dist_freshness.sh    # node_modules 已就绪时跳过安装
#
# 接入点：
#   - pre-push 的 PUSH_FULL_TESTS=1 全集路径（pytest 全绿后执行）
#   - 手动 / CI 发布前校验
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND="$ROOT/web/frontend"

[ -f "$FRONTEND/package.json" ] || {
    echo "[dist-check] 未找到 $FRONTEND/package.json" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || {
    echo "[dist-check] npm 未安装，无法构建前端（Node.js 为构建前置依赖）" >&2; exit 1; }

echo "[dist-check] 构建前端..."
cd "$FRONTEND"
if [ "${SKIP_NPM_CI:-0}" != "1" ]; then
    if [ -f package-lock.json ]; then npm ci; else npm install; fi
fi
npm run build

[ -f "$ROOT/web/dist/index.html" ] || {
    echo "[dist-check] 构建产物缺失：web/dist/index.html" >&2; exit 1; }

cd "$ROOT"
# --porcelain 同时覆盖：已跟踪文件修改/删除 + 内容哈希改名产生的未跟踪新产物
if [ -n "$(git status --porcelain -- web/dist)" ]; then
    echo "[dist-check] 错误：web/dist 与前端源码不同步——完整重建产生了差异：" >&2
    git status --porcelain -- web/dist >&2 | head -20
    echo "修复：git add web/dist && git commit 后再推送" >&2
    exit 1
fi
echo "[dist-check] web/dist 与前端源码同步 ✓"
