#!/usr/bin/env bash
# Release 前自动检查清单
# 用法:
#   ./scripts/release_checklist.sh          # 仅检查 (CI 模式，发现不一致非零退出)
#   ./scripts/release_checklist.sh --fix    # 以 VERSION 为准自动同步其他 3 个文件
#   ./scripts/release_checklist.sh --help   # 显示帮助
#
# 检查项:
#   1. 版本号四源一致性校验 (VERSION / pyproject.toml / .version / web/frontend/package.json)
#
# 后续可扩展:
#   - README 数值与实际统计一致性 (scripts/count_project_stats.py --check-readme)
#   - Docker compose 安全配置校验
#   - 测试覆盖率门槛
#
# 退出码:
#   0 = 全部检查通过
#   非 0 = 有检查项失败 (具体原因见输出)

set -e

# ---------- 路径处理 (兼容 Linux/macOS/Windows Git Bash) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------- 颜色输出 ----------
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
    GREEN="$(tput setaf 2)"
    YELLOW="$(tput setaf 3)"
    RED="$(tput setaf 1)"
    BLUE="$(tput setaf 4)"
    RESET="$(tput sgr0)"
else
    GREEN="" YELLOW="" RED="" BLUE="" RESET=""
fi

# ---------- 帮助信息 ----------
show_help() {
    cat <<EOF
${BLUE}xiaoda-agent Release Checklist${RESET}

用法:
  $0              仅检查 (CI 模式，发现不一致非零退出)
  $0 --fix        以 VERSION 为准自动同步其他 3 个文件
  $0 --help       显示此帮助信息

检查项:
  [1] 版本号四源一致性校验
      VERSION / pyproject.toml / .version / web/frontend/package.json

退出码:
  0 = 全部通过
  非 0 = 有检查项失败

后续可扩展检查:
  - README 数值一致性 (scripts/count_project_stats.py --check-readme)
  - Docker compose 安全配置
  - 测试覆盖率门槛
EOF
}

# ---------- 参数解析 ----------
MODE="ci"
for arg in "$@"; do
    case "$arg" in
        --fix)
            MODE="fix"
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo "${RED}未知参数: $arg${RESET}" >&2
            echo "使用 --help 查看帮助" >&2
            exit 2
            ;;
    esac
done

# ---------- 进入项目根目录 ----------
cd "$PROJECT_ROOT"

echo "${BLUE}=== xiaoda-agent Release Checklist ===${RESET}"
echo "Project root: $PROJECT_ROOT"
echo "Mode: $MODE"
echo ""

# ---------- 检查 1: 版本号四源一致性 ----------
echo "${BLUE}[1/1] 版本号四源一致性校验...${RESET}"
if [ "$MODE" = "fix" ]; then
    python scripts/check_version_sync.py --fix
else
    python scripts/check_version_sync.py --ci
fi
echo ""

# ---------- 后续可扩展检查 (取消注释以启用) ----------
# echo "${BLUE}[2/N] README 数值一致性...${RESET}"
# python scripts/count_project_stats.py --check-readme
# echo ""

# echo "${BLUE}[3/N] Docker compose 配置校验...${RESET}"
# docker compose -f docker-compose.prod.yml config >/dev/null
# echo ""

echo "${GREEN}=== All checks passed ===${RESET}"
