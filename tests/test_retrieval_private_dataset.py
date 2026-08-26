import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from db.database import DatabaseManager
from evaluation.retrieval_pipeline_harness import (
    CASE_BEHAVIOR_GATES,
    EXECUTED_CASE_IDS,
    EXECUTION_PLAN,
    SKIPPED_CASE_IDS,
    PipelineHostUnavailable,
    PipelineRetrievalAdapter,
    assert_query_cache_roundtrip,
    build_retrieval_host,
    insert_evaluation_fixtures,
)
from memory.scope import Scope
from web.routers.retrieval import evaluate_retrieval

DATASET = (
    Path(__file__).resolve().parents[1]
    / "evaluation"
    / "datasets"
    / "retrieval_zh_private_v1.json"
)


def test_private_retrieval_dataset_has_stable_schema_and_coverage():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    assert data["version"] == "xiaoda-retrieval-zh-v1"
    cases = data["cases"]
    assert len(cases) >= 12
    assert len({case["id"] for case in cases}) == len(cases)

    categories = {case["category"] for case in cases}
    assert {
        "exact_identifier",
        "semantic_rewrite",
        "coreference",
        "temporal",
        "negation_and_current_fact",
        "multi_hop",
        "conflict",
        "unanswerable",
        "scope_isolation",
        "group_scope_isolation",
        "mixed_zh_code",
        "typo_and_alias",
    } <= categories

    for case in cases:
        assert case["query"].strip()
        scope = case["scope"]
        assert scope["user_id"] and scope["agent_id"]
        relevance = case["expect_relevance"]
        assert isinstance(relevance, dict)
        assert all(
            isinstance(evidence_id, str)
            and isinstance(grade, (int, float))
            and not isinstance(grade, bool)
            and math.isfinite(float(grade))
            and grade >= 0
            for evidence_id, grade in relevance.items()
        )
        assert isinstance(case.get("executable_gate"), bool)
        if case["executable_gate"]:
            fixture_ids = {
                fixture["evidence_id"] for fixture in data["evidence_fixtures"]
            }
            assert set(relevance) <= fixture_ids
            assert set(case.get("forbidden_evidence_ids", [])) <= fixture_ids
        if case["unanswerable"]:
            assert relevance == {}
        else:
            assert any(grade > 0 for grade in relevance.values())


def test_execution_plan_classifies_every_dataset_case():
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    dataset_ids = [case["id"] for case in data["cases"]]
    assert set(EXECUTION_PLAN) == set(dataset_ids), (
        "执行计划必须覆盖数据集每个案例（新增类别未登记会被此断言拦截）"
    )

    gated_ids = {case["id"] for case in data["cases"] if case["executable_gate"]}
    executed_ids = set(EXECUTED_CASE_IDS)
    assert gated_ids <= executed_ids, (
        f"冻结 executable_gate=true 的案例不允许降级为跳过: {sorted(gated_ids - executed_ids)}"
    )

    for case_id in SKIPPED_CASE_IDS:
        plan = EXECUTION_PLAN[case_id]
        assert plan.skip_reason.strip(), f"{case_id}: 跳过必须显式登记原因"

    dataset_category_by_id = {case["id"]: case["category"] for case in data["cases"]}
    for case_id, plan in EXECUTION_PLAN.items():
        assert plan.category == dataset_category_by_id[case_id], (
            f"{case_id}: 执行计划类别与数据集不一致"
        )


def _forbidden_disjoint_gate(case: dict, rows: dict[str, int],
                             payload: dict) -> None:
    forbidden_rows = {rows[eid] for eid in case.get("forbidden_evidence_ids", [])}
    if not forbidden_rows:
        return
    returned = {result["id"] for result in payload["results"]}
    leaked = returned & forbidden_rows
    assert not leaked, (
        f"{case['id']}: 越权返回禁止证据（scope 隔离回归）: {leaked}"
    )


async def test_frozen_dataset_executes_through_full_retrieval_pipeline(
        tmp_path, request):
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    manager = DatabaseManager(tmp_path / "retrieval_private_eval.db")
    await manager.init()
    try:
        try:
            host = build_retrieval_host(manager)
        except PipelineHostUnavailable as exc:
            pytest.skip(
                "完整 RetrievalEngine 管线在当前环境不可用，"
                f"管线级评测显式跳过而非静默降级: {exc}")

        stable_to_row = await insert_evaluation_fixtures(host, data)
        adapter = PipelineRetrievalAdapter(host)

        cases_by_id = {case["id"]: case for case in data["cases"]}
        cases = []
        for case_id in EXECUTED_CASE_IDS:
            case = cases_by_id[case_id]
            cases.append({
                "query": case["query"],
                "scope": case["scope"],
                "expect_relevance": {
                    str(stable_to_row[evidence_id]): grade
                    for evidence_id, grade in case["expect_relevance"].items()
                },
                "unanswerable": case["unanswerable"],
            })

        app_state = SimpleNamespace(memory=adapter)
        # B5 根治：本用例验证的是「数据集跑通完整检索管线」，与 HTTP 层无关。
        # 原先在 async 测试内启 Starlette TestClient（portal 自建事件循环），
        # 与外层 pytest-asyncio loop 冲突，在 pytest-randomly 特定顺序下死锁
        # （线程栈停在 event loop select()，portal 死等，60s 超时）。
        # /retrieval/evaluate 的 HTTP 契约由 tests/test_retrieval_metrics.py 的
        # 同步 TestClient 用例覆盖；这里直接调用端点协程，复用外层事件循环。
        fake_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(core=app_state)))
        envelope = await evaluate_retrieval({
            "mode": "full",
            "scope": {"user_id": "eval-alice", "agent_id": "xiaoda"},
            "top_k": 5,
            "cases": cases,
        }, fake_request)
        payload = envelope.data
        assert payload["cases_failed"] == 0, (
            f"管线级评测存在失败案例: "
            f"{[c for c in payload['cases'] if not c.get('metrics')]}"
        )
        assert len(payload["cases"]) == len(EXECUTED_CASE_IDS)

        by_query = {case["query"]: case for case in payload["cases"]}
        notes: dict[str, str] = {}
        failures: list[str] = []
        for case_id in EXECUTED_CASE_IDS:
            case = cases_by_id[case_id]
            result = by_query[case["query"]]
            try:
                _forbidden_disjoint_gate(case, stable_to_row, result)
                note_parts = []
                for gate in CASE_BEHAVIOR_GATES.get(case_id, []):
                    note = gate(result, stable_to_row, case)
                    if note:
                        note_parts.append(note)
                top_id = next(
                    (eid for eid, rid in stable_to_row.items()
                     if result["results"] and result["results"][0]["id"] == rid),
                    None)
                notes[case_id] = "; ".join(note_parts + [
                    f"returned={len(result['results'])}",
                    f"score_kind={result['results'][0]['score_kind'] if result['results'] else '-'}",
                    f"top1={top_id or '-'}",
                ])
            except AssertionError as exc:
                failures.append(str(exc))

        cache_note = await assert_query_cache_roundtrip(
            host, "我的证件号码是多少",
            Scope(user_id="eval-alice", agent_id="xiaoda"))
        notes["<query_cache>"] = cache_note

        summary_lines = ["category|case|status|detail"]
        for case in data["cases"]:
            case_id = case["id"]
            plan = EXECUTION_PLAN[case_id]
            if plan.executable:
                detail = notes.get(case_id, "-")
                status = "EXECUTED"
            else:
                status = "SKIPPED"
                detail = plan.skip_reason
            summary_lines.append(f"{plan.category}|{case_id}|{status}|{detail}")
        summary_lines.append(
            f"统计: EXECUTED={len(EXECUTED_CASE_IDS)} "
            f"SKIPPED={len(SKIPPED_CASE_IDS)} cases_failed=0 | "
            + "; ".join(
                f"{EXECUTION_PLAN[cid].category}=SKIP({cid})"
                for cid in SKIPPED_CASE_IDS))
        summary = "\n".join(summary_lines)
        print(f"\n=== 检索冻结集管线级评测汇总 ===\n{summary}")
        request.node.add_report_section(
            "call", "retrieval_eval_summary", summary)

        assert not failures, f"管线级行为断言失败 {len(failures)} 项:\n" + "\n".join(failures)

        # prompt 组装确定性样本：真实管线结果 → EvidenceBundle → to_prompt
        # 非空（补齐“冻结集缺 prompt 注入路径真实命中样本”缺口，纯确定性无 LLM）
        from memory.evidence import EvidenceBundle, RetrievalPlan

        sample = next(
            (c for c in payload["cases"] if c.get("results")), None)
        assert sample is not None, "无任何带结果的 case 可作 prompt 组装样本"
        src_case = cases_by_id[
            next(cid for cid in EXECUTED_CASE_IDS
                 if cases_by_id[cid]["query"] == sample["query"])]
        plan = RetrievalPlan.from_query(
            src_case["query"],
            scope=Scope(user_id="eval-alice", agent_id="xiaoda"),
            top_k=5, enabled_channels=set(), budget_ms=8000,
        )
        bundle = EvidenceBundle.from_results(
            plan, sample["results"]).apply_budget(3000)
        assert bundle.evidence, f"{src_case['id']}: bundle 无证据"
        rendered = bundle.to_prompt()
        assert rendered and "retrieved_evidence" in rendered
        assert bundle.evidence[0].evidence_id in rendered
    finally:
        await manager.close()
