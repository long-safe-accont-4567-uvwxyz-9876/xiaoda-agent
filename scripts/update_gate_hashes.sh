#!/usr/bin/env bash
# 重生成门禁完整性台账(scripts/gate_integrity.txt)。
# 何时运行:正当修改了被锁定的门禁脚本/基线文件之后。
# 提交说明必须注明修改理由——hash 变更 + 无理由 = 审计红旗。
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'EOF'
import hashlib
from pathlib import Path

FILES = [
    "scripts/check_giant_files.sh",
    "scripts/check_todo_ratchet.sh",
    "scripts/check_broad_except.sh",
    "scripts/check_ruff.sh",
    "scripts/check_lazy_imports.py",
    "scripts/debt_audit.sh",
    "scripts/git-hooks/pre-push",
    "scripts/giant_file_allowlist.txt",
    "tests/test_giant_file_ratchet.py",
]

lines = ["# gate_integrity.txt — 门禁自身完整性台账(sha256)",
         "# 由 scripts/update_gate_hashes.sh 生成;debt_audit.sh 每周比对。",
         "# 正当修改门禁后必须重跑 update_gate_hashes.sh 并在提交说明注明理由;",
         "# 台账 hash 与实际不符 = 门禁被绕过/篡改的强信号。"]
for f in FILES:
    h = hashlib.sha256(Path(f).read_bytes()).hexdigest()
    lines.append(f"{h}  {f}")
Path("scripts/gate_integrity.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("gate_integrity.txt 已重生成")
EOF
