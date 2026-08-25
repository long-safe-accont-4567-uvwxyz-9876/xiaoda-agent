#!/usr/bin/env bash
# 通用巨型文件门禁（2026-08-25 技术债防复发体系第三层；同日扩展覆盖前端+测试）
#
# 规则：文件超过对应阈值即失败，除非出现在 giant_file_allowlist.txt 赦免清单。
#   - 后端生产 .py（排除 tests/chaos/scripts/evaluation/vendor）：900 行
#   - 前端 .ts/.vue（web/frontend/src，排除 node_modules）：900 行
#     （i18n 双字典 zh.ts/en.ts 是键值对数据表,天然豁免）
#   - 测试 .py：1500 行（测试天然偏长,阈值放宽;防 fixture 堆肥失控）
# 与 test_giant_file_ratchet.py 的分工：
#   - 本脚本：通用阈值，抓"新造巨型文件"（白名单制的盲区）；
#   - ratchet 测试：对已入赦免清单的热点钉死当前基线，只许拆小。
set -euo pipefail
cd "$(dirname "$0")/.."

MAX_PY="${GIANT_FILE_MAX_LINES:-900}"
MAX_WEB="${GIANT_FILE_WEB_MAX_LINES:-900}"
MAX_TEST="${GIANT_FILE_TEST_MAX_LINES:-1500}"
ALLOWLIST="scripts/giant_file_allowlist.txt"
exit_code=0

scan() {  # scan <描述> <find参数...> -- <阈值>
    local desc="$1"; shift
    local threshold="${@: -1}"
    local files=("${@:2:$#-2}")
    local f
    for f in "${files[@]}"; do
        [ -f "$f" ] || continue
        grep -qxF "$f" "$ALLOWLIST" 2>/dev/null && continue
        local lines
        lines=$(wc -l < "$f")
        if [ "$lines" -gt "$threshold" ]; then
            echo "[giant-file] ✗ [$desc] $f ${lines} 行 > ${threshold} 行阈值且未在 $ALLOWLIST" >&2
            exit_code=1
        fi
    done
}

mapfile -t py_files < <(find . -name "*.py" \
    -not -path "./.venv/*" -not -path "./.git/*" -not -path "*/__pycache__/*" \
    -not -path "./web/*" -not -path "./vendor/*" \
    -not -path "./tests/*" -not -path "./chaos/*" -not -path "./scripts/*" \
    -not -path "./evaluation/*" -not -path "./*.egg-info*" -not -path "./xiaoda-agent/*" \
    | sed 's|^\./||' | sort)
mapfile -t web_files < <(find web/frontend/src \( -name "*.ts" -o -name "*.vue" \) 2>/dev/null | sort)
mapfile -t test_files < <(find tests -name "*.py" 2>/dev/null | sort)

scan "后端py" "${py_files[@]}" -- "$MAX_PY"
scan "前端TS/Vue" "${web_files[@]}" -- "$MAX_WEB"
scan "测试py" "${test_files[@]}" -- "$MAX_TEST"

if [ "$exit_code" = "1" ]; then
    echo "[giant-file] 处理方式:拆分该文件;或确属合理(i18n键值表/纯数据清单/append-only注册表等)," >&2
    echo "[giant-file]           登记进 $ALLOWLIST 并同步在 tests/test_giant_file_ratchet.py 钉基线," >&2
    echo "[giant-file]           提交说明注明理由。" >&2
    exit 1
fi

echo "[giant-file] OK(py ${MAX_PY} / web ${MAX_WEB} / test ${MAX_TEST})"
