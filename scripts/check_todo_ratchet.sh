#!/usr/bin/env bash
# TODO/FIXME/HACK 棘轮(2026-08-25 技术债防复发体系)
#
# 规则:非测试生产源码中的 TODO/FIXME/HACK 标记计数冻结在
#      scripts/todo_baseline.txt;新增即拦。想留债?可以——但必须:
#      1) 上调基线并在提交说明写明是什么债、为什么现在不还;
#      2) 基线只许随债务清偿下调。
# 这把"沉默的以后再说"变成"显式登记的已知债务"。
set -euo pipefail
cd "$(dirname "$0")/.."

BASELINE_FILE="scripts/todo_baseline.txt"
BASELINE=$(tail -1 "$BASELINE_FILE")

COUNT=$(grep -rEn "\b(TODO|FIXME|HACK)\b" --include="*.py" . 2>/dev/null \
    | grep -v "\.venv\|\.git/\|__pycache__\|node_modules\|/tests/\|vendor\|web/dist\|\.egg-info" \
    | wc -l)

if [ "$COUNT" -gt "$BASELINE" ]; then
    echo "[todo-ratchet] ✗ TODO/FIXME/HACK 计数 ${COUNT} > 基线 ${BASELINE}" >&2
    echo "[todo-ratchet]   当前全部标记(对照基线找出新增项):" >&2
    grep -rEn "\b(TODO|FIXME|HACK)\b" --include="*.py" . 2>/dev/null \
        | grep -v "\.venv\|\.git/\|__pycache__\|node_modules\|/tests/\|vendor\|web/dist\|\.egg-info" >&2 || true
    echo "[todo-ratchet]   处理:当场还债;或上调 $BASELINE_FILE 并在提交说明登记债务理由。" >&2
    exit 1
fi

echo "[todo-ratchet] OK(${COUNT}/${BASELINE})"
