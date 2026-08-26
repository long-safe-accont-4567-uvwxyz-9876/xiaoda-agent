"""Prompt A/B 评测 harness：vN vs vN+1 程序化指标对比与 promote 门禁。

harness 不调用模型：调用方分别用两个 prompt 版本渲染并执行同一批
PromptABCase，把输出文本交给 compare_runs()。所有指标确定性可复现，
本地/API 后端各跑一次并以 backend 标签分开出分。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from memory.knowledge_graph import _clean_json_response, _repair_json


@dataclass(frozen=True)
class PromptABCase:
    case_id: str
    variables: dict[str, str]
    required_fields: tuple[str, ...] = ()
    expect_contains: tuple[str, ...] = ()
    expect_absent: tuple[str, ...] = ()
    evidence_quote_field: str = ""
    evidence_source_variable: str = ""
    evidence_list_field: str = ""

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("prompt AB case_id is required")
        if bool(self.evidence_quote_field) != bool(self.evidence_source_variable):
            raise ValueError("evidence quote check requires both field and source variable")
        if self.evidence_source_variable and self.evidence_source_variable not in self.variables:
            raise ValueError(f"evidence source variable missing: {self.evidence_source_variable}")
        if self.evidence_list_field and not self.evidence_quote_field:
            raise ValueError("evidence_list_field requires evidence_quote_field")
        if self.evidence_list_field and self.evidence_list_field not in (
            self.required_fields or ()
        ):
            raise ValueError("evidence_list_field must be declared in required_fields")


@dataclass(frozen=True)
class PromptABOutcome:
    case_id: str
    schema_ok: bool
    missing_fields: tuple[str, ...] = ()
    missed_golds: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    bad_quotes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.schema_ok
            and not self.missed_golds
            and not self.violations
            and not self.bad_quotes
        )

    def summary(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "schema_ok": self.schema_ok,
            "missing_fields": list(self.missing_fields),
            "missed_golds": list(self.missed_golds),
            "violations": list(self.violations),
            "bad_quotes": list(self.bad_quotes),
        }


def _severity(outcome: PromptABOutcome) -> tuple[int, int, int, int]:
    return (
        0 if outcome.schema_ok else 1,
        len(outcome.missed_golds),
        len(outcome.violations),
        len(outcome.bad_quotes),
    )


def _parse_model_json(output: str) -> Any:
    """与生产 KG 解析同源容错：剥围栏 → json.loads → 修复回退。"""
    cleaned = _clean_json_response(output)
    try:
        return json.loads(cleaned)
    except (TypeError, ValueError):
        try:
            return json.loads(_repair_json(cleaned))
        except (TypeError, ValueError):
            return None


def evaluate_output(case: PromptABCase, output: str) -> PromptABOutcome:
    parsed: Any = None
    schema_ok = True
    missing: tuple[str, ...] = ()
    if case.required_fields:
        parsed = _parse_model_json(output)
        if parsed is None:
            schema_ok = False
        elif not isinstance(parsed, dict):
            schema_ok = False
            parsed = None
        else:
            missing = tuple(f for f in case.required_fields if f not in parsed)
            schema_ok = not missing
    searchable = output
    if isinstance(parsed, dict):
        searchable = json.dumps(parsed, ensure_ascii=False)
    missed = tuple(g for g in case.expect_contains if g not in searchable)
    violations = tuple(b for b in case.expect_absent if b in searchable)
    bad_quotes: tuple[str, ...] = ()
    if case.evidence_quote_field and isinstance(parsed, dict):
        source_text = case.variables[case.evidence_source_variable]
        if case.evidence_list_field:
            items = parsed.get(case.evidence_list_field)
            raw_quotes: list[Any] = []
            if isinstance(items, list) and items:
                for item in items:
                    if isinstance(item, dict):
                        raw_quotes.append(item.get(case.evidence_quote_field))
                    else:
                        raw_quotes.append(None)
            else:
                bad_quotes += ("<missing list>",)
                raw_quotes = []
        else:
            raw = parsed.get(case.evidence_quote_field)
            raw_quotes = list(raw) if isinstance(raw, list) else [raw]
        for quote in raw_quotes:
            text = str(quote or "")
            if not text or text not in source_text:
                bad_quotes += (text,)
    return PromptABOutcome(
        case_id=case.case_id,
        schema_ok=schema_ok,
        missing_fields=missing,
        missed_golds=missed,
        violations=violations,
        bad_quotes=bad_quotes,
    )


def _run_report(
    label: str,
    outputs: dict[str, str],
    cases: tuple[PromptABCase, ...],
) -> dict[str, Any]:
    outcomes = {
        case.case_id: evaluate_output(case, outputs.get(case.case_id, ""))
        for case in cases
    }
    total = len(cases)
    schema_ok_count = sum(1 for o in outcomes.values() if o.schema_ok)
    golden_ok_count = sum(1 for o in outcomes.values() if not o.missed_golds)
    violation_count = sum(len(o.violations) for o in outcomes.values())
    return {
        "label": label,
        "schema_rate": schema_ok_count / total if total else 1.0,
        "golden_rate": golden_ok_count / total if total else 1.0,
        "violation_count": violation_count,
        "all_ok": all(o.ok for o in outcomes.values()),
        "outcomes": {cid: o.summary() for cid, o in outcomes.items()},
    }


def _diff_outcomes(
    baseline: dict[str, PromptABOutcome],
    candidate: dict[str, PromptABOutcome],
) -> tuple[list[str], list[str]]:
    regressions: list[str] = []
    improvements: list[str] = []
    for case_id, base in baseline.items():
        cand = candidate.get(case_id)
        if cand is None:
            continue
        if _severity(cand) < _severity(base):
            improvements.append(case_id)
        elif _severity(cand) > _severity(base):
            regressions.append(case_id)
    return regressions, improvements


def compare_runs(
    cases: tuple[PromptABCase, ...],
    baseline_outputs: dict[str, str],
    candidate_outputs: dict[str, str],
    *,
    baseline_label: str = "",
    candidate_label: str = "",
) -> dict[str, Any]:
    """同一批 cases 上对比两个 prompt 版本的输出。

    baseline/candidate 输出以 case_id 为键；缺失的输出按空串评估，
    必然不满足 required_fields 与 expect_contains，视为最差结果。
    """
    report: dict[str, Any] = {
        "baseline": _run_report(baseline_label or "baseline", baseline_outputs, cases),
        "candidate": _run_report(candidate_label or "candidate", candidate_outputs, cases),
        "regressions": [],
        "improvements": [],
    }
    baseline_raw = {
        c.case_id: evaluate_output(c, baseline_outputs.get(c.case_id, "")) for c in cases
    }
    candidate_raw = {
        c.case_id: evaluate_output(c, candidate_outputs.get(c.case_id, "")) for c in cases
    }
    regressions, improvements = _diff_outcomes(baseline_raw, candidate_raw)
    report["regressions"] = regressions
    report["improvements"] = improvements
    return report


def promote_gate(
    report: dict[str, Any],
    *,
    min_schema_rate: float = 0.95,
    min_golden_rate: float = 1.0,
    max_violation_count: int = 0,
) -> tuple[bool, list[str]]:
    """candidate 版本转 production 的评测门禁（文档 §6.4）。

    默认策略：schema 合规率 >= 0.95；golden 字面量（人名/数字/否定/时间）
    保持率必须 100%；禁用内容零出现；相对基线零回归。
    """
    candidate = report["candidate"]
    reasons: list[str] = []
    if candidate["schema_rate"] < min_schema_rate:
        reasons.append(
            f"schema_rate {candidate['schema_rate']:.3f} < {min_schema_rate}"
        )
    if candidate["golden_rate"] < min_golden_rate:
        reasons.append(
            f"golden_rate {candidate['golden_rate']:.3f} < {min_golden_rate}"
        )
    if candidate["violation_count"] > max_violation_count:
        reasons.append(
            f"violation_count {candidate['violation_count']} > {max_violation_count}"
        )
    if report["regressions"]:
        reasons.append(f"regressions vs baseline: {report['regressions']}")
    return (not reasons, reasons)


def _union_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def merge_run_summaries(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合多次跑分的报告为一次保守合并结论。

    合并语义（对候选与基线同样适用）：某 case 仅在全部通过才算 ok；
    missing/missed/violation/bad_quotes 取跨次并集——偶发失败即视为
    不稳定，门禁据此拒绝，避免单次抽签式晋级。
    """
    if not reports:
        raise ValueError("no runs to merge")
    sides = ("baseline", "candidate")
    merged_outcomes: dict[str, dict[str, dict[str, Any]]] = {side: {} for side in sides}
    first = reports[0]
    for side in sides:
        case_ids = first[side]["outcomes"].keys()
        for cid in case_ids:
            runs = [r[side]["outcomes"][cid] for r in reports]
            fields = ("missing_fields", "missed_golds", "violations", "bad_quotes")
            merged_outcomes[side][cid] = {
                "case_id": cid,
                "ok": all(o["ok"] for o in runs),
                "schema_ok": all(o["schema_ok"] for o in runs),
                **{
                    f: _union_preserving_order(
                        [item for o in runs for item in o[f]]
                    )
                    for f in fields
                },
            }
    merged: dict[str, Any] = {}
    for side in sides:
        outcomes = merged_outcomes[side]
        total = len(outcomes)
        merged[side] = {
            "label": first[side].get("label", side),
            "schema_rate": (
                sum(1 for o in outcomes.values() if o["schema_ok"]) / total
                if total else 1.0
            ),
            "golden_rate": (
                sum(1 for o in outcomes.values() if not o["missed_golds"]) / total
                if total else 1.0
            ),
            "violation_count": sum(len(o["violations"]) for o in outcomes.values()),
            "all_ok": all(o["ok"] for o in outcomes.values()),
            "outcomes": outcomes,
        }
    base_sev = {
        cid: (
            0 if o["schema_ok"] else 1,
            len(o["missed_golds"]),
            len(o["violations"]),
            len(o["bad_quotes"]),
        )
        for cid, o in merged_outcomes["baseline"].items()
    }
    regressions: list[str] = []
    improvements: list[str] = []
    for cid, cand in merged_outcomes["candidate"].items():
        base = base_sev.get(cid)
        if base is None:
            continue
        cand_sev = (
            0 if cand["schema_ok"] else 1,
            len(cand["missed_golds"]),
            len(cand["violations"]),
            len(cand["bad_quotes"]),
        )
        if cand_sev < base:
            improvements.append(cid)
        elif cand_sev > base:
            regressions.append(cid)
    return {
        "baseline": merged["baseline"],
        "candidate": merged["candidate"],
        "regressions": regressions,
        "improvements": improvements,
    }
