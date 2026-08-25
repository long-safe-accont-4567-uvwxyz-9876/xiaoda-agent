#!/usr/bin/env bash
# 技术债周审计(2026-08-25 防复发体系)——由 debt-audit.timer 每周触发。
#
# 目的:提交门禁(pre-push)可被 --no-verify / 直接改库绕过;本审计独立于
# 提交流程重跑全部棘轮,保证任何漏网债务存活期 ≤ 一周,并留下时间戳台账。
# 结果追加到 data/debt_audit.log(外挂盘),有失败即 exit 1(systemd 记 failure)。
set -uo pipefail
cd "$(dirname "$0")/.."

LOG="${DEBT_AUDIT_LOG:-data/debt_audit.log}"
mkdir -p "$(dirname "$LOG")"
{
    echo "════ 债务审计 $(date '+%F %T') HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo 'n/a') ════"
} >> "$LOG"

fail=0
run() {  # run <名称> <命令...>
    local name="$1"; shift
    if out=$("$@" 2>&1); then
        echo "  ✓ $name: $out" | tail -1 >> "$LOG"
    else
        echo "  ✗ $name:" >> "$LOG"
        echo "$out" | head -10 | sed 's/^/      /' >> "$LOG"
        fail=1
    fi
}

run "巨型文件门禁"   bash scripts/check_giant_files.sh
run "TODO棘轮"       bash scripts/check_todo_ratchet.sh
run "except棘轮"     bash scripts/check_broad_except.sh
run "ruff棘轮"       bash scripts/check_ruff.sh
run "lazy-import棘轮" .venv/bin/python scripts/check_lazy_imports.py

# 棘轮基线漂移检测:赦免清单文件的实际行数应与 ratchet 基线一致
drift=$(python3 - <<'EOF'
import re
from pathlib import Path
ratchet = Path("tests/test_giant_file_ratchet.py").read_text(encoding="utf-8")
baselines = dict(re.findall(r'"([^"]+)":\s*(\d+)', ratchet))
problems = []
for f, base in baselines.items():
    actual = len(Path(f).read_text(encoding="utf-8").splitlines())
    if actual != int(base):
        problems.append(f"{f}: ratchet基线{base} vs 实际{actual}")
print("; ".join(problems) if problems else "OK")
EOF
) || drift="检测脚本异常"
echo "$drift" | grep -q "^OK" && echo "  ✓ 棘轮基线对齐: $drift" >> "$LOG" || {
    echo "  ✗ 棘轮基线漂移: $drift" >> "$LOG"; fail=1; }

# 全集测试(慢,约6分钟;审计窗口无所谓)
if pytest_out=$(.venv/bin/python -m pytest tests/ -q --timeout=120 \
        -m "not slow and not e2e_real" -p no:cacheprovider 2>&1 | tail -1); then
    echo "  ✓ 全集: $pytest_out" >> "$LOG"
else
    echo "  ✗ 全集: $pytest_out" >> "$LOG"
    fail=1
fi

{
    [ "$fail" = "0" ] && echo "── 结论:全绿 ──" || echo "── 结论:存在违规,见上 ──"
} >> "$LOG"
exit "$fail"
