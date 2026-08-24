"""Prompt 治理 API 契约测试：profile 概览、stage/门禁 promote/rollback、A/B 评测端点。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web import config_service as config_service_module
from web.config_service import ConfigService
from web.routers.auth import get_current_user
from web.routers.local_deploy import router as local_deploy_router


@pytest.fixture()
def api(tmp_path, monkeypatch):
    import web.prompt_audit as prompt_audit_module
    from web.prompt_audit import PromptAuditLog

    config = ConfigService(tmp_path / "webui_overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", config)
    monkeypatch.setattr(prompt_audit_module, "_module_log",
                        PromptAuditLog(tmp_path / "audit.jsonl"))
    app = FastAPI()
    app.include_router(local_deploy_router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.state.core = None
    with TestClient(app) as client:
        yield client


MD_BASELINE = {
    "md.name_number_kept": "小林花了1280元买了新显卡，周末要装机。",
    "md.negation_allergy_kept": "用户对花生过敏，绝对不能吃含花生的东西。",
    "md.time_place_kept": "2024年3月从北京搬去杭州，养了叫煤球的猫。",
    "md.no_structured_header": "用户加班到晚上10点，抱怨需求反复变更。",
    "md.units_kept": "9月5日：用户说体重降到72.5公斤了，每天走8000步。",
    "md.negation_variant_qianwan": "9月8日：用户说加班时千万别再喝咖啡，上次喝了心悸。",
    "md.toolchain_mixed_language": "9月10日：用户在 VSCode 里配好了 ruff 和 pytest，说以后提交前都跑一遍。",
}
MD_GOOD_CANDIDATE = {
    "md.name_number_kept": "小林花1280元买了显卡，计划周末装机。",
    "md.negation_allergy_kept": "用户花生过敏，不能吃含花生的食物。",
    "md.time_place_kept": "2024年3月搬到杭州，养猫煤球。",
    "md.no_structured_header": "用户加班到22点，对需求反复变更感到疲惫。",
    "md.units_kept": "体重降到72.5公斤，每天走8000步。",
    "md.negation_variant_qianwan": "加班时千万别喝咖啡，喝了会心悸。",
    "md.toolchain_mixed_language": "VSCode 里配好 ruff 和 pytest，提交前跑一遍。",
}
MD_BAD_CANDIDATE = {
    "md.name_number_kept": "有人买了显卡。",  # 丢人名丢数字
    **{k: v for k, v in MD_GOOD_CANDIDATE.items() if k != "md.name_number_kept"},
}


def test_get_profiles_returns_summaries_and_golden_cases(api):
    resp = api.get("/local-deploy/prompt-profiles/memory_distill")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["node_id"] == "memory_distill"
    prompt_ids = {p["prompt_id"] for p in data["profiles"]}
    assert {"memory.compress_episode", "memory.build_recall_note"} <= prompt_ids
    assert all(p["status"] == "production" for p in data["profiles"])
    assert all(not p["staged"] and not p["overridden"] for p in data["profiles"])
    case_ids = {c["case_id"] for c in data["golden_cases"]}
    assert "md.name_number_kept" in case_ids


def test_get_profiles_unknown_or_nongenerative_node_404(api):
    assert api.get("/local-deploy/prompt-profiles/no_such_node").status_code == 404
    assert api.get("/local-deploy/prompt-profiles/embedding").status_code == 404


def test_stage_rejects_invalid_record_then_accepts_valid(api):
    bad = api.post(
        "/local-deploy/prompt-profiles/memory.compress_episode/stage",
        json={"version": "2.0.0", "output_schema": {"type": "nonsense"}},
    )
    assert bad.status_code == 422

    ok = api.post(
        "/local-deploy/prompt-profiles/memory.compress_episode/stage",
        json={
            "version": "2.0.0",
            "user_template": "压缩记忆并保留数字与否定：{memories_text}",
            "variables": {"memories_text": {"required": True}},
            "output_schema": {"type": "string"},
        },
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "staging"

    overview = api.get("/local-deploy/prompt-profiles/memory_distill").json()["data"]
    staged = next(p for p in overview["profiles"]
                  if p["prompt_id"] == "memory.compress_episode")
    assert staged["staged"] is True


def test_stage_rejects_unrenderable_template_with_clear_error(api):
    """stage 试渲染门禁：坏模板 422 + 明确 error，不入库（staged 保持 false）。"""
    bad = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/stage",
        json={
            "version": "2.0.0",
            "system_template": "你是{period_text}的回忆整理助手",
            "user_template": "{period_text}",
            "variables": {"period_text": {"required": True}},
            "output_schema": {"type": "string"},
        },
    )
    assert bad.status_code == 422
    assert "trial render" in bad.json()["detail"]
    overview = api.get("/local-deploy/prompt-profiles/memory_distill").json()["data"]
    staged = next(p for p in overview["profiles"]
                  if p["prompt_id"] == "memory.build_recall_note")
    assert staged["staged"] is False


def test_ab_eval_reports_gate_verdict(api):
    good = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/ab-eval",
        json={"baseline_outputs": MD_BASELINE, "candidate_outputs": MD_GOOD_CANDIDATE},
    )
    assert good.status_code == 200
    payload = good.json()["data"]
    assert payload["node_id"] == "memory_distill"
    assert payload["gate"]["passed"] is True
    assert payload["report"]["candidate"]["golden_rate"] == 1.0
    assert payload["report"]["regressions"] == []

    bad = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/ab-eval",
        json={"baseline_outputs": MD_BASELINE, "candidate_outputs": MD_BAD_CANDIDATE},
    )
    payload = bad.json()["data"]
    assert payload["gate"]["passed"] is False
    assert any("schema_rate" not in r or "golden" in r or "regressions" in r
               for r in payload["gate"]["reasons"])
    assert "md.name_number_kept" in payload["report"]["regressions"]

    assert api.post(
        "/local-deploy/prompt-profiles/no.such.prompt/ab-eval",
        json={"baseline_outputs": {}, "candidate_outputs": {}},
    ).status_code == 404
    assert api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/ab-eval",
        json={"baseline_outputs": None},
    ).status_code == 422


def test_promote_requires_passing_report_and_rollback_restores(api):
    hdr = {"X-Confirm": "yes"}
    stage = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/stage",
        json={
            "version": "2.0.0",
            "user_template": "回忆笔记 v2：{period_text}",
            "variables": {"period_text": {"required": True}},
            "output_schema": {"type": "string"},
        },
    )
    assert stage.status_code == 200

    eval_good = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/ab-eval",
        json={"baseline_outputs": MD_BASELINE, "candidate_outputs": MD_GOOD_CANDIDATE},
    ).json()["data"]
    promoted = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/promote",
        json={"ab_report": eval_good["report"]},
        headers=hdr,
    )
    assert promoted.status_code == 200
    assert promoted.json()["data"]["version"] == "2.0.0"

    stage_v3 = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/stage",
        json={
            "version": "3.0.0",
            "user_template": "回忆笔记 v3：{period_text}",
            "variables": {"period_text": {"required": True}},
            "output_schema": {"type": "string"},
        },
    )
    assert stage_v3.status_code == 200
    eval_bad = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/ab-eval",
        json={"baseline_outputs": MD_BASELINE, "candidate_outputs": MD_BAD_CANDIDATE},
    ).json()["data"]
    blocked = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/promote",
        json={"ab_report": eval_bad["report"]},
        headers=hdr,
    )
    assert blocked.status_code == 422
    assert "gate rejected" in blocked.json()["detail"]

    overview = api.get("/local-deploy/prompt-profiles/memory_distill").json()["data"]
    note_profile = next(p for p in overview["profiles"]
                        if p["prompt_id"] == "memory.build_recall_note")
    assert note_profile["overridden"] is True
    assert note_profile["staged"] is True

    rolled_back = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/rollback",
        headers=hdr,
    )
    assert rolled_back.status_code == 409


def test_prompt_promote_and_rollback_require_confirm_header(api):
    base = "/local-deploy/prompt-profiles/memory.build_recall_note"
    # 破坏类接口必须校验 X-Confirm: yes（与 agents/system 路由同款防护）
    assert api.post(f"{base}/promote", json={}).status_code == 400
    assert api.post(f"{base}/rollback").status_code == 400
    bad_hdr = {"X-Confirm": "no"}
    assert api.post(f"{base}/promote", json={}, headers=bad_hdr).status_code == 400
    assert api.post(f"{base}/rollback", headers=bad_hdr).status_code == 400


def test_rollback_chain_preserves_intermediate_versions(api):
    """连续回滚不得丢版本：被替换的 production 必须压回 history。"""
    hdr = {"X-Confirm": "yes"}
    base = "/local-deploy/prompt-profiles/memory.build_recall_note"
    for v in ("2.0.0", "3.0.0", "4.0.0"):
        stage = api.post(
            f"{base}/stage",
            json={
                "version": v,
                "user_template": "回忆笔记 " + v + "：{period_text}",
                "variables": {"period_text": {"required": True}},
                "output_schema": {"type": "string"},
            },
        )
        assert stage.status_code == 200
        promoted = api.post(f"{base}/promote", json={}, headers=hdr)
        assert promoted.status_code == 200

    seen_versions = [
        api.post(f"{base}/rollback", headers=hdr).json()["data"]["version"]
        for _ in range(4)
    ]
    # prod=v4, history=[v2,v3]：unshift+pop 轮转 → v3 → v2 → v4 → v3
    assert seen_versions == ["3.0.0", "2.0.0", "4.0.0", "3.0.0"]

    overview = api.get("/local-deploy/prompt-profiles/memory_distill").json()["data"]
    note_profile = next(p for p in overview["profiles"]
                        if p["prompt_id"] == "memory.build_recall_note")
    assert note_profile["overridden"] is True


def test_ab_run_rejects_runs_with_multi_backend_combination(api):
    resp = api.post(
        "/local-deploy/prompt-profiles/memory.build_recall_note/ab-run",
        json={"backends": ["api", "local"], "runs": 3},
    )
    assert resp.status_code == 422
    assert "not supported" in resp.json()["detail"]


def test_promote_without_report_remits_legacy_behavior(api):
    stage = api.post(
        "/local-deploy/prompt-profiles/memory.compress_episode/stage",
        json={
            "version": "2.0.0",
            "user_template": "{memories_text}",
            "variables": {"memories_text": {"required": True}},
            "output_schema": {"type": "string"},
        },
    )
    assert stage.status_code == 200
    promoted = api.post(
        "/local-deploy/prompt-profiles/memory.compress_episode/promote",
        json={},
        headers={"X-Confirm": "yes"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["data"]["status"] == "production"


def test_audit_log_records_governance_events(tmp_path, monkeypatch):
    import web.prompt_ab_runner as runner_module
    import web.prompt_audit as prompt_audit_module
    from web.prompt_audit import PromptAuditLog

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(prompt_audit_module, "_module_log",
                        PromptAuditLog(audit_path))

    async def fake_invoke(system: str, user: str) -> str:
        return user

    def fake_builder(backend, core, *, node_local_model=None, model=None):
        return fake_invoke, []

    monkeypatch.setattr(runner_module, "build_backend_model", fake_builder)
    config = ConfigService(tmp_path / "webui_overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", config)
    app = FastAPI()
    app.include_router(local_deploy_router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.state.core = None
    with TestClient(app) as client:
        client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/stage",
            json={
                "version": "2.0.0",
                "user_template": "{memories_text}",
                "variables": {
                    "memories_text": {"required": True},
                    "n": {"required": False},
                },
                "output_schema": {"type": "string"},
            },
        )
        run = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/ab-run",
            json={"backends": ["api"]},
        )
        assert run.status_code == 200
        client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/promote",
            json={"ab_report": run.json()["data"]["sweeps"][0]["report"]},
            headers={"X-Confirm": "yes"},
        )
        audit = client.get(
            "/local-deploy/prompt-profiles/memory.compress_episode/audit"
        )
        assert audit.status_code == 200
        events = audit.json()["data"]

    assert [e["event"] for e in events] == ["stage", "ab_run", "promote"]
    assert events[0]["version"] == "2.0.0"
    assert events[1]["backends"] == ["api"]
    assert events[1]["gate_passed"] is True
    assert "api" in events[1]["rates"]
    assert events[2]["gated"] is True
    assert all(e["ts"] > 0 for e in events)
