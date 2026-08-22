#!/bin/bash
# =============================================================================
# 宽口径 except 棘轮检查（2026-08-22 技术债四大专项 #2）
# 背景：全仓存量 except Exception 共 1136 处（非测试源码），逐点收窄是
#       多会话专项；本脚本把存量冻结为基线，只拦增量——债不再增长。
# 规则：当前计数 > 基线 → 失败。确需新增时，先收窄异常类型；
#       实属必要的宽捕获，随提交同步上调 scripts/broad_except_baseline.txt。
# 基线只许下调或伴随净修复上调，禁止无说明拔高。
# =============================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

BASELINE_FILE="scripts/broad_except_baseline.txt"
BASELINE=$(cat "$BASELINE_FILE" 2>/dev/null || echo 0)
COUNT=$(grep -rn "except Exception" --include="*.py" . 2>/dev/null \
    | grep -v "/\.git/\|/tests/\|/\.venv/\|/build/\|/dist/" | wc -l)

echo "[broad-except] 当前 $COUNT / 基线 $BASELINE"
if [ "$COUNT" -gt "$BASELINE" ]; then
    echo "✗ 检测到新增宽口径 except（+$((COUNT - BASELINE))）。" >&2
    echo "  请优先收窄为具体异常类型（OSError/ValueError/json.JSONDecodeError 等）；" >&2
    echo "  确属必要宽捕获时，同步上调 $BASELINE_FILE 并在提交说明中给出理由。" >&2
    exit 1
fi
