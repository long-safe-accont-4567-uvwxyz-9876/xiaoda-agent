"""真实模型 A/B runner 契约测试：内置渲染、staged 渲染、fake 模型全量跑分与门禁。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web import config_service as config_service_module
from web.config_service import ConfigService
from web.prompt_ab_runner import render_builtin_templates, run_prompt_ab
from web.prompt_profile_repository import PromptProfileRepository
from web.routers.auth import get_current_user
from web.routers.local_deploy import router as local_deploy_router


class FakeRouter:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def route(self, task_type, messages, temperature=0.7,
                    max_tokens=None, **kwargs):
        self.calls.append({"task_type": task_type, "messages": messages})
        return self.reply(messages[-1]["content"])


def _keep_literal_reply(content: str) -> str:
    if "花生" in content and "过敏" in content:
        return "用户对花生过敏，不能吃含花生的食物。"
    if "杭州" in content:
        return "2024年3月搬到杭州，养了叫煤球的猫。"
    if "72.5" in content:
        return "用户体重降到72.5公斤，每天走8000步。"
    if "心悸" in content or ("咖啡" in content and "千万" in content):
        return "用户说加班时千万别再喝咖啡，上次喝了心悸。"
    if "VSCode" in content:
        return "用户在 VSCode 里配好了 ruff 和 pytest，以后提交前都跑一遍。"
    if "加班" in content or "需求" in content:
        return "用户加班到很晚，对需求反复变更感到疲惫。"
    if "1280" in content and "小林" in content:
        return "小林花了1280元买了显卡，计划周末装机。"
    return "有人最近买了硬件。"


@pytest.mark.asyncio
async def test_render_builtin_templates_substitutes_known_tokens(tmp_path):
    system, user = render_builtin_templates(
        "memory.compress_episode", {"memories_text": "小林花了1280元买显卡"}
    )
    assert system == ""
    assert "小林花了1280元买显卡" in user


def test_render_builtin_rejects_unbound_or_unknown_prompt():
    with pytest.raises(ValueError, match="no bound builtin"):
        render_builtin_templates("nudge.generate", {})
    with pytest.raises(ValueError, match="unknown prompt profile"):
        render_builtin_templates("no.such", {})


@pytest.mark.asyncio
async def test_run_prompt_ab_without_staging_marks_gate_pending(tmp_path):
    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    router = FakeRouter(_keep_literal_reply)
    core = SimpleNamespace(router=router)

    result = await run_prompt_ab(core, repository, "memory.compress_episode")

    assert result["node_id"] == "memory_distill"
    report = result["report"]
    assert report["baseline"]["golden_rate"] == 1.0
    assert report["candidate"]["golden_rate"] == 1.0
    # 未 stage 候选时 candidate==baseline，"全绿"是空洞结论：
    # 显式标记且门禁置为 pending（未通过），不得据此 promote
    assert result["no_candidate_under_test"] is True
    assert result["gate"]["passed"] is False
    assert any("no candidate" in r for r in result["gate"]["reasons"])
    assert all(call["task_type"] == "chat" for call in router.calls)
    assert len(router.calls) == 7


def _stage_valid_candidate(repository) -> None:
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


@pytest.mark.asyncio
async def test_run_prompt_ab_staged_candidate_regression_blocks_gate(tmp_path):
    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    repository.stage({
        "prompt_id": "memory.compress_episode",
        "version": "2.0.0",
        "user_template": "V2忽略内容直接输出泛化摘要",
        "variables": {"memories_text": {"required": False}, "n": {"required": False}},
        "output_schema": {"type": "string"},
    })
    router = FakeRouter(_keep_literal_reply)
    core = SimpleNamespace(router=router)

    result = await run_prompt_ab(core, repository, "memory.compress_episode")

    cand = result["report"]["candidate"]
    assert cand["golden_rate"] < 1.0
    assert result["report"]["regressions"]
    assert result["gate"]["passed"] is False


@pytest.mark.asyncio
async def test_run_prompt_ab_staged_variable_mismatch_raises(tmp_path):
    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    repository.stage({
        "prompt_id": "memory.compress_episode",
        "version": "2.0.0",
        "user_template": "{other_var}",
        "variables": {"other_var": {"required": True}},
        "output_schema": {"type": "string"},
    })

    with pytest.raises(ValueError):
        await run_prompt_ab(
            SimpleNamespace(router=FakeRouter(lambda _: "x")),
            repository, "memory.compress_episode",
        )


@pytest.mark.asyncio
async def test_run_prompt_ab_injected_model_bypasses_router(tmp_path):
    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    _stage_valid_candidate(repository)

    async def model(system: str, user: str) -> str:
        return _keep_literal_reply(user)

    result = await run_prompt_ab(
        None, repository, "memory.compress_episode", model=model,
    )

    assert result["report"]["baseline"]["label"] == "builtin"
    assert result["backend"] == "current"
    assert result["no_candidate_under_test"] is False
    assert result["gate"]["passed"] is True


@pytest.mark.asyncio
async def test_run_prompt_ab_api_backend_uses_free_model_seam(tmp_path, monkeypatch):
    import web.prompt_ab_runner as runner_module

    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    _stage_valid_candidate(repository)
    seen_users: list[str] = []

    async def fake_api_model(system: str, user: str) -> str:
        seen_users.append(user)
        return _keep_literal_reply(user)

    def fake_builder(backend, core, *, node_local_model=None, model=None):
        assert backend == "api"
        return fake_api_model, []

    monkeypatch.setattr(runner_module, "build_backend_model", fake_builder)

    result = await runner_module.run_prompt_ab(
        None, repository, "memory.compress_episode", backend="api",
    )

    assert result["backend"] == "api"
    assert result["gate"]["passed"] is True
    assert any("小林" in u for u in seen_users)


@pytest.mark.asyncio
async def test_sweep_runs_each_backend_and_requires_all_gates(tmp_path, monkeypatch):
    import web.prompt_ab_runner as runner_module
    from web.prompt_ab_runner import run_prompt_ab_sweep

    config = ConfigService(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    _stage_valid_candidate(repository)
    invoked: list[str] = []

    def fake_builder(backend, core, *, node_local_model=None, model=None):
        async def invoke(system: str, user: str) -> str:
            invoked.append(backend)
            if backend == "local":
                return "有人买了东西。"
            return _keep_literal_reply(user)
        return invoke, []

    monkeypatch.setattr(runner_module, "build_backend_model", fake_builder)

    result = await run_prompt_ab_sweep(
        SimpleNamespace(router=None), repository,
        "memory.compress_episode", ("api", "local"),
    )

    assert result["backends"] == ["api", "local"]
    assert len(result["sweeps"]) == 2
    assert [s["backend"] for s in result["sweeps"]] == ["api", "local"]
    # 有 staged 候选：baseline 与 candidate 各跑一批
    assert invoked.count("api") == 14
    assert invoked.count("local") == 14
    assert result["gate"]["passed"] is False
    assert any("[local]" in r for r in result["gate"]["reasons"])
    assert not any("[api]" in r for r in result["gate"]["reasons"])


def _build_app(tmp_path, monkeypatch, core):
    import web.prompt_audit as prompt_audit_module
    from web.prompt_audit import PromptAuditLog

    config = ConfigService(tmp_path / "overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", config)
    monkeypatch.setattr(prompt_audit_module, "_module_log",
                        PromptAuditLog(tmp_path / "audit.jsonl"))
    app = FastAPI()
    app.include_router(local_deploy_router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.state.core = core
    return app


def test_ab_run_endpoint_executes_and_reports(tmp_path, monkeypatch):
    router = FakeRouter(_keep_literal_reply)
    app = _build_app(tmp_path, monkeypatch, core=SimpleNamespace(router=router))
    with TestClient(app) as client:
        stage = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/stage",
            json={
                "version": "2.0.0",
                "user_template": "{memories_text}",
                "variables": {"memories_text": {"required": True}, "n": {"required": False}},
                "output_schema": {"type": "string"},
            },
        )
        assert stage.status_code == 200
        resp = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/ab-run"
        )
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data["no_candidate_under_test"] is False
    assert data["gate"]["passed"] is True
    assert data["report"]["candidate"]["label"] == "candidate"


def test_ab_run_endpoint_guards(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, core=None)
    with TestClient(app) as client:
        assert client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/ab-run"
        ).status_code == 409
        assert client.post(
            "/local-deploy/prompt-profiles/nudge.generate/ab-run"
        ).status_code == 409
        assert client.post(
            "/local-deploy/prompt-profiles/no.such.prompt/ab-run"
        ).status_code == 404


def test_ab_run_endpoint_sweep_shape_and_validation(tmp_path, monkeypatch):
    import web.prompt_ab_runner as runner_module

    def fake_builder(backend, core, *, node_local_model=None, model=None):
        async def invoke(system: str, user: str) -> str:
            return _keep_literal_reply(user)
        return invoke, []

    monkeypatch.setattr(runner_module, "build_backend_model", fake_builder)
    app = _build_app(tmp_path, monkeypatch, core=SimpleNamespace(router=FakeRouter(lambda _: "x")))
    with TestClient(app) as client:
        stage = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/stage",
            json={
                "version": "2.0.0",
                "user_template": "{memories_text}",
                "variables": {"memories_text": {"required": True}, "n": {"required": False}},
                "output_schema": {"type": "string"},
            },
        )
        assert stage.status_code == 200, stage.json()
        ok = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/ab-run",
            json={"backends": ["api", "local"]},
        )
        assert ok.status_code == 200
        data = ok.json()["data"]
        assert data["backends"] == ["api", "local"]
        assert len(data["sweeps"]) == 2
        assert data["gate"]["passed"] is True
        flat = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/ab-run",
            json={},
        )
        assert flat.status_code == 200
        assert "report" in flat.json()["data"]
        bad = client.post(
            "/local-deploy/prompt-profiles/memory.compress_episode/ab-run",
            json={"backends": ["gpu"]},
        )
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_safe_invoke_enforces_timeout(monkeypatch):
    import web.prompt_ab_runner as runner_module

    started = asyncio.Event()

    async def slow_invoke(system: str, user: str) -> str:
        started.set()
        await asyncio.sleep(5)
        return "never"

    errors: list[str] = []
    monkeypatch.setattr(runner_module, "_INVOKE_TIMEOUT", 0.05)

    out = await runner_module._safe_invoke(slow_invoke, "s", "u", errors)

    assert out == ""
    assert any("timed out" in e for e in errors)


@pytest.mark.asyncio
async def test_ab_run_endpoint_rejects_duplicate_concurrent_run(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    import web.prompt_ab_runner as runner_module

    started = asyncio.Event()
    release = asyncio.Event()

    def fake_builder(backend, core, *, node_local_model=None, model=None):
        async def invoke(system: str, user: str) -> str:
            started.set()
            await release.wait()
            return _keep_literal_reply(user)
        return invoke, []

    monkeypatch.setattr(runner_module, "build_backend_model", fake_builder)
    app = _build_app(tmp_path, monkeypatch, core=SimpleNamespace(router="unused"))
    url = "/local-deploy/prompt-profiles/memory.compress_episode/ab-run"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post(url))
        await asyncio.wait_for(started.wait(), timeout=5)
        duplicate = await client.post(url)
        assert duplicate.status_code == 409
        assert "already in progress" in duplicate.json()["detail"]
        release.set()
        done = await first

    assert done.status_code == 200
    assert done.json()["data"]["no_candidate_under_test"] is True
