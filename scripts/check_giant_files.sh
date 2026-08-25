#!/usr/bin/env bash
# 通用巨型文件门禁（2026-08-25 技术债防复发体系第三层）
#
# 规则：任何非测试生产 .py 文件超过 MAX_LINES 行即失败，
#       除非出现在本目录 giant_file_allowlist.txt 赦免清单中。
# 与 test_giant_file_ratchet.py 的分工：
#   - 本脚本：通用阈值，抓"新造巨型文件"（白名单制的盲区）；
#   - ratchet 测试：对已入赦免清单的热点钉死当前基线，只许拆小。
# 新增巨型文件的正当出路：拆分，或给出理由后登记赦免清单（提交说明须注明）。
set -euo pipefail
cd "$(dirname "$0")/.."

MAX_LINES="${GIANT_FILE_MAX_LINES:-900}"
ALLOWLIST="scripts/giant_file_allowlist.txt"

violators=$(find . -name "*.py" \
    -not -path "./.venv/*" -not -path "./.git/*" -not -path "*/__pycache__/*" \
    -not -path "./web/frontend/*" -not -path "./vendor/*" \
    -not -path "./tests/*" -not -path "./chaos/*" -not -path "./scripts/*" \
    -not -path "./evaluation/*" -not -path "./*.egg-info*" \
    | xargs wc -l 2>/dev/null \
    | awk -v max="$MAX_LINES" '$1 > max && $2 != "total" {sub(/^\.\//, "", $2); print $2}' \
    | sort)

while IFS= read -r f; do
    [ -z "$f" ] && continue
    # 赦免清单中的文件交给 ratchet 棘轮管基线,此处跳过
    if grep -qxF "$f" "$ALLOWLIST" 2>/dev/null; then
        continue
    fi
    lines=$(wc -l < "$f")
    echo "[giant-file] ✗ $f ${lines} 行 > ${MAX_LINES} 行阈值且未在 $ALLOWLIST" >&2
    exit_code=1
done <<EOF
$violators
EOF

if [ "${exit_code:-0}" = "1" ]; then
    echo "[giant-file] 处理方式:拆分该文件;或确属合理(纯数据清单/append-only注册表等)," >&2
    echo "[giant-file]           登记进 $ALLOWLIST 并同步在 tests/test_giant_file_ratchet.py 钉基线," >&2
    echo "[giant-file]           提交说明注明理由。" >&2
    exit 1
fi

echo "[giant-file] OK(阈值 ${MAX_LINES} 行)"
