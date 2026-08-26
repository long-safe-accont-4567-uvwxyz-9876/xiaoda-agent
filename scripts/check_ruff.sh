#!/bin/bash
# =============================================================================
# ruff lint 棘轮检查（2026-08-25 技术债第二轮）
# 背景：2026-08-25 批量清理前全仓 ruff 错误曾积累到 1446 条（703 未排序
#       import / 275 未用导入 / 228 缺 EOF 换行……），一次性清至 149。
#       本脚本把存量冻结为基线，只拦增量——债不再增长。
# 规则：当前计数 > 基线 → 失败。修复后应同步下调基线；
#       确属必要的新增（如 noqa 无法覆盖的场景），随提交上调
#       scripts/ruff_baseline.txt 并在提交说明中给出理由。
# 基线只许下调或伴随净修复上调，禁止无说明拔高。
#
# 存量 149 的构成（有意保留，勿盲目清零）：
#   - 115 E402：config.py 导入副作用链的固有形态（sys.path/环境初始化先行）
#   - 34 测试内 F841：替身构造赋值，自动删除会丢失 setup 副作用
# =============================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "[ruff] 跳过：找不到 $PWD/.venv/bin/python" >&2
    exit 0
fi

BASELINE_FILE="scripts/ruff_baseline.txt"
BASELINE=$(cat "$BASELINE_FILE" 2>/dev/null || echo 0)
# concise 格式每条违规一行，行数即计数（比解析汇总行稳健）
COUNT=$("$PYTHON" -m ruff check --output-format concise . 2>/dev/null | grep -c ":" || true)
COUNT=${COUNT:-0}

echo "[ruff] 当前 $COUNT / 基线 $BASELINE"
if [ "$COUNT" -gt "$BASELINE" ]; then
    echo "✗ 检测到新增 ruff 违规（+$((COUNT - BASELINE))）。运行以下命令查看明细：" >&2
    echo "    .venv/bin/python -m ruff check ." >&2
    echo "  可自动修复的先跑：.venv/bin/python -m ruff check --fix ." >&2
    echo "  确属必要的新增时，同步上调 $BASELINE_FILE 并在提交说明中给出理由。" >&2
    exit 1
fi
