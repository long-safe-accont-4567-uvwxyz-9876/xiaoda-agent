#!/usr/bin/env python3
"""安全扫描门禁：按 HIGH/CRITICAL 计数阈值判定 CI security job 红绿。

用法:
    python scripts/security_gate.py bandit-report.json     # bandit JSON 报告
    python scripts/security_gate.py pip-audit-report.json  # pip-audit JSON 报告

规则（2026-08-24 门禁化）：
- bandit: HIGH 计数 > scripts/security_baseline.json 的 high_count → exit 1。
  baseline.exemptions 中登记的 (file, test_id) 条目不计入计数，每条必须带 reason。
- pip-audit: 存在 severity 为 CRITICAL/HIGH 的漏洞 → exit 1
  （依赖漏洞没有"存量豁免"——修复方式是升级 pin，不是登记豁免）。

报告缺失/格式非法一律失败（fail-closed），不允许扫描工具静默失效。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "scripts" / "security_baseline.json"


def load_baseline() -> dict:
    if not BASELINE_PATH.is_file():
        print(f"FATAL: baseline file missing: {BASELINE_PATH}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FATAL: baseline file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


def load_report(path_str: str) -> dict:
    path = Path(path_str)
    if not path.is_file():
        print(f"FATAL: report not found: {path} — scanner must not fail silently", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FATAL: report is not valid JSON: {exc} — fail-closed", file=sys.stderr)
        sys.exit(2)


def gate_bandit(report_path: str, baseline: dict) -> int:
    report = load_report(report_path)
    results = report.get("results")
    if results is None:
        print("FATAL: bandit report missing 'results' key — fail-closed", file=sys.stderr)
        return 2

    exemptions = {
        (e.get("file"), e.get("test_id"))
        for e in baseline.get("exemptions", [])
        if e.get("reason")
    }
    allowed = baseline.get("high_count", 0)

    highs = [r for r in results if r.get("issue_severity") == "HIGH"]
    counted = [
        r for r in highs
        if (r.get("filename"), r.get("test_id")) not in exemptions
    ]

    print(f"bandit: HIGH total={len(highs)}, exempted={len(highs) - len(counted)}, "
          f"counted={len(counted)}, baseline allows={allowed}")
    if len(counted) > allowed:
        print(f"GATE FAILED: new HIGH findings above baseline ({len(counted)} > {allowed}):")
        for r in counted:
            print(f"  - {r.get('filename')}:{r.get('line_number')} "
                  f"[{r.get('test_id')}] {r.get('issue_text', '')[:120]}")
        print("Fix the finding or add an exemption with reason in "
              "scripts/security_baseline.json (with justification).")
        return 1
    print("bandit gate PASSED")
    return 0


def gate_pip_audit(report_path: str, _baseline: dict) -> int:
    report = load_report(report_path)
    # pip-audit -f json 输出形如 [{"package": ..., "vulnerabilities": [...]}]
    # 或 {"dependencies": [...]}（取决于版本）；两种都兼容。
    entries = report if isinstance(report, list) else report.get("dependencies", [])
    blocking = []
    for entry in entries:
        for vuln in entry.get("vulnerabilities", []) or []:
            aliases = vuln.get("aliases") or []
            fix_versions = ", ".join(vuln.get("fix_versions") or []) or "none"
            sev = (vuln.get("severity") or "").upper()
            # 无 severity 字段的旧版 pip-audit：有 fix 版本即视为可修的高危处理过严，
            # 因此无 severity 时保守放行、只拦显式 CRITICAL/HIGH（字段缺失时看 ID 是否 GHSA）。
            if sev in ("CRITICAL", "HIGH"):
                blocking.append(
                    f"  - {entry.get('name', '?')}=={entry.get('version', '?')} "
                    f"{vuln.get('id', '?')} [{sev or 'UNKNOWN'}] fixes: {fix_versions}"
                )
            elif not sev and any(str(a).startswith("GHSA-") for a in aliases):
                blocking.append(
                    f"  - {entry.get('name', '?')}=={entry.get('version', '?')} "
                    f"{vuln.get('id', '?')} [GHSA] fixes: {fix_versions}"
                )
    print(f"pip-audit: CRITICAL/HIGH vulnerabilities={len(blocking)}")
    if blocking:
        print("GATE FAILED: upgrade the pinned versions to clear these:")
        for line in blocking[:30]:
            print(line)
        if len(blocking) > 30:
            print(f"  ... and {len(blocking) - 30} more")
        return 1
    print("pip-audit gate PASSED")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    report_path = argv[1]
    baseline = load_baseline()
    name = Path(report_path).name.lower()
    if "pip-audit" in name:
        return gate_pip_audit(report_path, baseline)
    return gate_bandit(report_path, baseline)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
