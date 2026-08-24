import asyncio

from agent_context import AgentContext
from memory.context_usage import estimate_token_count
from memory.evidence import (
    EvidenceBundle,
    RetrievalPlan,
    validate_citations,
)
from memory.scope import Scope


def _result(memory_id, summary, score, **extra):
    return {
        "id": memory_id,
        "summary": summary,
        "final_score": score,
        "version": extra.pop("version", 1),
        "content_hash": extra.pop("content_hash", f"hash-{memory_id}"),
        "timestamp": extra.pop("timestamp", 1000.0 + memory_id),
        **extra,
    }


def test_evidence_bundle_adapts_legacy_results_with_stable_ids():
    scope = Scope(user_id="alice", agent_id="xiaoda")
    plan = RetrievalPlan.from_query("我的偏好", scope=scope, top_k=5)
    bundle = EvidenceBundle.from_results(
        plan,
        [_result(7, "用户不吃香菜", 0.9, source="fts", score_kind="source")],
    )

    evidence = bundle.evidence[0]
    assert evidence.evidence_id == "M:episodic:7:v1"
    assert evidence.scope.user_id == scope.user_id
    assert evidence.scope.agent_id == scope.agent_id
    assert evidence.original_text == "用户不吃香菜"
    assert evidence.content_hash == "hash-7"
    assert evidence.channels == ("fts",)
    assert bundle.to_dict()["evidence"][0]["evidence_id"] == evidence.evidence_id


def test_evidence_budget_keeps_highest_scores_and_records_drops():
    plan = RetrievalPlan.from_query("q", scope=Scope(), top_k=5)
    bundle = EvidenceBundle.from_results(
        plan,
        [
            _result(1, "高分短证据", 0.9),
            _result(2, "中分证据" * 40, 0.5),
            _result(3, "低分证据" * 40, 0.1),
        ],
    )

    budgeted = bundle.apply_budget(max_tokens=100)

    assert [item.source_id for item in budgeted.evidence] == ["1"]
    assert {item.source_id for item in budgeted.dropped} == {"2", "3"}
    assert all(item.reason == "token_budget" for item in budgeted.dropped)
    assert budgeted.injected_tokens <= 100
    assert estimate_token_count(budgeted.to_prompt()) <= 100
    assert budgeted.retrieved_tokens >= budgeted.injected_tokens


def test_bundle_records_missing_empty_and_duplicate_drops():
    plan = RetrievalPlan.from_query("q", scope=Scope(), top_k=5)
    bundle = EvidenceBundle.from_results(plan, [
        {"summary": "missing id"},
        {"id": 1, "summary": ""},
        _result(2, "重复证据", 0.9),
        _result(2, "重复证据", 0.8),
        _result(3, "重复证据", 0.7),
    ])

    assert [item.source_id for item in bundle.evidence] == ["2"]
    reasons = {item.reason for item in bundle.dropped}
    assert reasons == {
        "missing_source_id", "empty_text", "duplicate_id", "duplicate_content"
    }


def test_bundle_merges_upstream_drop_trace():
    bundle = EvidenceBundle.from_results(
        RetrievalPlan.from_query("q", scope=Scope(), top_k=1),
        [_result(1, "kept", 0.9)],
        upstream_dropped=(("2", "top_k"), ("3", "low_score")),
    )
    assert {(item.source_id, item.reason) for item in bundle.dropped} == {
        ("2", "top_k"), ("3", "low_score")
    }


def test_kg_relation_conflict_and_provenance_are_derived_from_production_fields():
    bundle = EvidenceBundle.from_results(
        RetrievalPlan.from_query("q", scope=Scope(), top_k=5),
        [
            {
                "id": "REL-1", "summary": "用户住在北京",
                "evidence_type": "kg_v2_relation", "from_entity": "用户",
                "relation_type": "住在", "episode_ids": '["EP-1"]',
                "valid_at": 1000, "invalid_at": 2000, "is_current": 0,
            },
            {
                "id": "REL-2", "summary": "用户住在上海",
                "evidence_type": "kg_v2_relation", "from_entity": "用户",
                "relation_type": "住在", "episode_ids": '["EP-2"]',
                "valid_at": 2000, "invalid_at": None, "is_current": 1,
            },
        ],
    )

    assert bundle.conflicts[0].conflict_key == "用户:住在"
    assert bundle.conflicts[0].preferred_evidence_id.startswith("KG:relation:REL-2")
    assert bundle.evidence[0].provenance_ids == ("EP-1",)
    assert bundle.evidence[0].version.startswith("h")


def test_conflict_groups_prefer_current_fact_but_keep_history():
    plan = RetrievalPlan.from_query("我现在住哪里", scope=Scope(), top_k=5)
    bundle = EvidenceBundle.from_results(
        plan,
        [
            _result(
                1, "用户住在北京", 0.5, conflict_key="residence",
                valid_at=1000.0, invalid_at=2000.0, is_current=0,
            ),
            _result(
                2, "用户住在上海", 0.9, conflict_key="residence",
                valid_at=2000.0, invalid_at=None, is_current=1,
            ),
        ],
    )

    assert len(bundle.conflicts) == 1
    conflict = bundle.conflicts[0]
    assert conflict.preferred_evidence_id == "M:episodic:2:v1"
    assert set(conflict.evidence_ids) == {
        "M:episodic:1:v1", "M:episodic:2:v1"
    }


def test_agent_context_shadow_bundle_is_user_isolated_and_clearable():
    context = AgentContext(system_prompt="test")
    alice_token = asyncio.run(context.switch_user_context("alice"))
    bundle = EvidenceBundle.from_results(
        RetrievalPlan.from_query(
            "q", scope=Scope(user_id="alice"), top_k=5
        ),
        [_result(1, "Alice evidence", 0.9)],
    )
    assert asyncio.run(
        context.commit_user_context(alice_token, evidence_bundle=bundle)
    ) is True

    asyncio.run(context.switch_user_context("bob"))
    assert context.evidence_bundle is None
    asyncio.run(context.switch_user_context("alice"))
    assert context.evidence_bundle.evidence[0].source_id == "1"
    context.clear()
    assert context.evidence_bundle is None


async def test_memory_manager_materializes_request_trace_on_results():
    from types import SimpleNamespace

    from memory.memory_manager import MemoryManager
    from memory.retrieval.trace import (
        begin_retrieval_trace,
        mark_retrieval_degraded,
        mark_retrieval_dropped,
    )

    async def retrieve(*args, **kwargs):
        begin_retrieval_trace()
        mark_retrieval_degraded("reranker")
        mark_retrieval_dropped(9, "low_score")
        return [{"id": 1, "summary": "kept"}]

    manager = MemoryManager.__new__(MemoryManager)
    manager._retrieval = SimpleNamespace(retrieve_memories=retrieve)
    results = await manager.retrieve_memories("q", scope=Scope())

    assert results[0]["degraded_components"] == ["reranker"]
    assert results[0]["retrieval_dropped"] == [("9", "low_score")]


async def test_traced_retrieval_preserves_drops_when_results_are_empty():
    from types import SimpleNamespace

    from memory.memory_manager import MemoryManager
    from memory.retrieval.trace import (
        begin_retrieval_trace,
        mark_retrieval_dropped,
    )

    async def retrieve(*args, **kwargs):
        begin_retrieval_trace()
        mark_retrieval_dropped(9, "low_score")
        return []

    manager = MemoryManager.__new__(MemoryManager)
    manager._retrieval = SimpleNamespace(retrieve_memories=retrieve)
    outcome = await manager.retrieve_memories_with_trace("q", scope=Scope())

    assert outcome.results == ()
    assert outcome.dropped == (("9", "low_score"),)


def test_citation_validation_rejects_wrong_claim_with_valid_id():
    bundle = EvidenceBundle.from_results(
        RetrievalPlan.from_query("q", scope=Scope(), top_k=5),
        [_result(1, "用户住在上海", 0.9)],
    )
    report = validate_citations("用户住在北京 [M:episodic:1:v1]。", bundle)

    assert report.valid is False
    assert report.citation_precision == 0.0
    assert report.citation_recall == 0.0
    assert report.unsupported_claims


def test_citation_validation_rejects_changed_numeric_literal():
    bundle = EvidenceBundle.from_results(
        RetrievalPlan.from_query("q", scope=Scope(), top_k=5),
        [_result(5, "订单总金额为1000元人民币", 0.9)],
    )
    report = validate_citations(
        "订单总金额为9000元人民币 [M:episodic:5:v1]。", bundle
    )
    assert report.valid is False
    assert report.unsupported_claims


def test_citation_validation_rejects_unknown_and_reports_missing_claims():
    plan = RetrievalPlan.from_query("q", scope=Scope(), top_k=5)
    bundle = EvidenceBundle.from_results(
        plan, [_result(1, "用户住在上海", 0.9)]
    )
    text = (
        "用户住在上海 [M:episodic:1:v1]。\n"
        "用户喜欢滑雪 [M:episodic:999:v1]。\n"
        "用户明天去旅行。"
    )

    report = validate_citations(text, bundle)

    assert report.valid_ids == ("M:episodic:1:v1",)
    assert report.unknown_ids == ("M:episodic:999:v1",)
    assert "用户明天去旅行。" in report.uncited_claims
    assert report.valid is False


def test_bundle_renders_into_memory_retrieval_block():
    """闭环回归：context.evidence_bundle 必须渲染进 _format_memory_retrieval 输出。"""
    import asyncio

    from memory.evidence import EvidenceBundle, RetrievalPlan
    from memory.scope import Scope
    plan = RetrievalPlan.from_query("测试问题", scope=Scope(), top_k=5)
    bundle = EvidenceBundle.from_results(plan, [{
        "content": "用户住在杭州", "source_channel": "vector",
        "score": 0.9, "user_id": "default", "agent_id": "xiaoda",
        "session_id": "user", "timestamp": 1.0,
    }])
    from agent_context import AgentContext
    ctx = AgentContext.__new__(AgentContext)
    ctx.memory_retrieval = [{"type": "distilled", "content": "用户住在杭州"}]
    ctx.evidence_bundle = None
    base = ctx._format_memory_retrieval()
    assert "<retrieved_evidence" not in base
    ctx.evidence_bundle = bundle
    out = ctx._format_memory_retrieval()
    assert "<retrieved_evidence" in out and "untrusted" in out
    # 空/超预算 bundle（prompt_enabled=False）不渲染
    ctx.evidence_bundle = EvidenceBundle.from_results(plan, []).apply_budget(0)
    assert "<retrieved_evidence" not in ctx._format_memory_retrieval()
