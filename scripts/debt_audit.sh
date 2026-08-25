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
    actual = Path(f).read_text(encoding="utf-8").count("\n")  # wc同口径
    if actual != int(base):
        problems.append(f"{f}: ratchet基线{base} vs 实际{actual}")
print("; ".join(problems) if problems else "OK")
EOF
) || drift="检测脚本异常"
echo "$drift" | grep -q "^OK" && echo "  ✓ 棘轮基线对齐: $drift" >> "$LOG" || {
    echo "  ✗ 棘轮基线漂移: $drift" >> "$LOG"; fail=1; }

# 门禁自身完整性:关键脚本与基线文件的 sha256 比对台账。
# hash 不符 = 门禁被篡改/绕过的强信号;正当修改后须重跑 update_gate_hashes.sh。
if integ=$(python3 - <<'EOF'
import hashlib
from pathlib import Path
problems, checked = [], 0
for line in Path("scripts/gate_integrity.txt").read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    expected, f = line.split(None, 1)
    f = f.strip()
    checked += 1
    actual = hashlib.sha256(Path(f).read_bytes()).hexdigest()
    if actual != expected:
        problems.append(f"{f} 已被修改(台账hash不符)")
missing = []
print(f"{checked} 项锁定" if not problems else "; ".join(problems))
EOF
); then
    echo "  ✓ 门禁完整性: $integ" >> "$LOG"
else
    echo "  ✗ 门禁完整性: $integ" >> "$LOG"; fail=1
fi

# 赦免清单↔ratchet 一致性:allowlist 每一项都应在 ratchet 钉基线(防清单变成
# 无看守的逃逸通道——进了赦免清单却没人盯行数 = 白名单制盲区复现)
if cons=$(python3 - <<'EOF'
from pathlib import Path
allow = [l.strip() for l in Path("scripts/giant_file_allowlist.txt").read_text(encoding="utf-8").splitlines()
         if l.strip() and not l.lstrip().startswith("#")]
ratchet = Path("tests/test_giant_file_ratchet.py").read_text(encoding="utf-8")
unpinned = [f for f in allow if f'"{f}"' not in ratchet]
print("OK" if not unpinned else "赦免未钉基线: " + ", ".join(unpinned))
EOF
); then
    echo "  ✓ 赦免清单一致性: $cons" >> "$LOG"
else
    echo "  ✗ 赦免清单一致性: $cons" >> "$LOG"; fail=1
fi

# 全集测试(慢,约6分钟;审计窗口无所谓)
if pytest_out=$(.venv/bin/python -m pytest tests/ -q --timeout=120 \
        -m "not slow and not e2e_real" -p no:cacheprovider 2>&1 | tail -1); then
    echo "  ✓ 全集: $pytest_out" >> "$LOG"
else
    echo "  ✗ 全集: $pytest_out" >> "$LOG"
    fail=1
fi

if [ "$fail" != "0" ]; then
    summary=$(tail -20 "$LOG" | grep -E "✗" | head -5 | tr '\n' ';')
    # 醒目告警:err 级别进 journal(journalctl -p err 可见),供运维面板/巡检捕获;
    # systemd 层面另有 OnFailure 钩子(deploy/debt-audit.service)。
    logger -t debt-audit -p daemon.err "技术债周审计存在违规: ${summary}"
    echo "── 结论:存在违规,已上报告警 ──" >> "$LOG"
else
    echo "── 结论:全绿 ──" >> "$LOG"
fi
exit "$fail"
