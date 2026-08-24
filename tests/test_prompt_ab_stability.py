"""N-run 聚合门禁契约测试：偶发失败视为不稳定，拒绝抽签式通过。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from web.prompt_ab import merge_run_summaries
from web.prompt_ab_runner import run_prompt_ab_multi


def _summary(case_id, *, ok=True, schema_ok=True, missed=(), viol=(),
             missing=()):
    return {
        "case_id": case_id,
        "ok": ok,
        "schema_ok": schema_ok,
        "missing_fields": list(missing),
        "missed_golds": list(missed),
        "violations": list(viol),
        "bad_quotes": [],
    }


def _report(outcomes):
    return {
        "baseline": {
            "label": "builtin", "schema_rate": 1.0, "golden_rate": 1.0,
            "violation_count": 0, "all_ok": True, "outcomes": outcomes,
        },
        "candidate": {
            "label": "candidate", "schema_rate": 1.0, "golden_rate": 1.0,
            "violation_count": 0, "all_ok": True, "outcomes": outcomes,
        },
        "regressions": [], "improvements": [],
    }


def test_merge_flags_flaky_case_as_unstable():
    runs = []
    for i in range(3):
        outcomes = {
            "a": _summary("a"),
            "b": _summary("b", ok=(i != 1), schema_ok=(i != 1),
                          missed=() if i != 1 else ("1280",)),
        }
        rep = _report(outcomes)
        rep["candidate"]["golden_rate"] = 1.0 if i != 1 else 0.5
        rep["candidate"]["all_ok"] = i != 1
        runs.append(rep)

    merged = merge_run_summaries(runs)

    cand_b = merged["candidate"]["outcomes"]["b"]
    assert cand_b["ok"] is False
    assert cand_b["missed_golds"] == ["1280"]
    assert merged["candidate"]["golden_rate"] == 0.5
    assert merged["regressions"] == []


def test_merge_unions_violations_across_runs():
    runs = [
        _report({"x": _summary("x")}),
        _report({"x": _summary("x", ok=False, viol=("bad1",))}),
        _report({"x": _summary("x", ok=False, schema_ok=False,
                               missing=("primary",))}),
    ]
    for rep in runs:
        rep["candidate"]["outcomes"] = {
            cid: dict(o) for cid, o in rep["candidate"]["outcomes"].items()
        }

    merged = merge_run_summaries(runs)

    x = merged["candidate"]["outcomes"]["x"]
    assert x["violations"] == ["bad1"]
    assert x["missing_fields"] == ["primary"]
    assert x["schema_ok"] is False and x["ok"] is False
    assert merged["candidate"]["schema_rate"] == 0.0


@pytest.mark.asyncio
async def test_multi_run_gate_blocks_when_any_round_fails(tmp_path, monkeypatch):
    import web.prompt_ab_runner as runner_module
    from web.config_service import ConfigService
    from web.prompt_profile_repository import PromptProfileRepository

    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    repository.stage({
        "prompt_id": "memory.compress_episode",
        "version": "2.0.0",
        "user_template": "{memories_text}",
        "variables": {
            "memories_text": {"required": True},
            "n": {"required": False},
        },
        "output_schema": {"type": "string"},
    })

    cand_seen: dict[str, int] = {}

    async def flaky_model(system: str, user: str) -> str:
        from tests.test_prompt_ab_runner import _keep_literal_reply

        if "记忆蒸馏助手" in user:
            return _keep_literal_reply(user)
        cand_seen[user] = cand_seen.get(user, 0) + 1
        if cand_seen[user] >= 2:
            return "有人买了东西。"
        return _keep_literal_reply(user)

    monkeypatch.setattr(runner_module, "build_backend_model",
                        lambda backend, core, node_local_model=None, model=None:
                        (flaky_model, []))

    result = await runner_module.run_prompt_ab_multi(
        None, repository, "memory.compress_episode", runs=2,
    )

    assert result["runs"] == 2
    assert result["per_run_candidate_all_ok"] == [True, False]
    assert result["gate"]["passed"] is False


@pytest.mark.asyncio
async def test_multi_run_rejects_invalid_runs(tmp_path):
    from web.config_service import ConfigService
    from web.prompt_profile_repository import PromptProfileRepository

    repository = PromptProfileRepository(ConfigService(tmp_path / "o.json"))
    with pytest.raises(ValueError):
        await run_prompt_ab_multi(None, repository, "memory.compress_episode", runs=0)
    with pytest.raises(ValueError):
        await run_prompt_ab_multi(None, repository, "memory.compress_episode", runs=6)


@pytest.mark.asyncio
async def test_multi_run_infra_failure_raises_instead_of_poisoning(tmp_path, monkeypatch):
    import web.prompt_ab_runner as runner_module
    from web.config_service import ConfigService
    from web.prompt_profile_repository import PromptProfileRepository

    repository = PromptProfileRepository(ConfigService(tmp_path / "o.json"))
    calls = {"n": 0}

    async def infra_flaky(system: str, user: str) -> str:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated 429")
        return "小林花了1280元买了显卡。"

    monkeypatch.setattr(runner_module, "build_backend_model",
                        lambda backend, core, node_local_model=None, model=None:
                        (infra_flaky, []))

    with pytest.raises(RuntimeError, match="model infra failure"):
        await runner_module.run_prompt_ab_multi(
            None, repository, "memory.compress_episode", runs=2,
        )
