from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.intent_decomposition import IntentDecomposer
from local_ai.integration.reranker import LocalRerankerService
from web.config_service import ConfigService
from web.local_deploy_nodes import (
    apply_to_runtime,
    model_has_other_local_references,
    validate_local_selection,
)
from web.node_registry import NODES, set_backend
from web.prompt_profile_repository import PromptProfileRepository
from web.prompt_profiles import profiles_for_node
from web.routers.auth import get_current_user
from web.routers.local_deploy import router as local_deploy_router

EXPECTED_NODE_IDS = {
    "embedding",
    "reranker",
    "query_transform",
    "instinct",
    "error_rule",
    "kg_extract",
    "asr",
    "emotion_llm",
    "portrait",
    "nudge",
    "reunion",
    "growth",
    "memory_distill",
    "spontaneous_recall",
    "dream",
    "intent_decomposition",
}


def test_functional_node_registry_is_complete_and_self_describing(tmp_path):
    by_id = {node["id"]: node for node in NODES}
    assert set(by_id) == EXPECTED_NODE_IDS
    for node in by_id.values():
        assert node["capability"]
        assert node["runtime_adapter"]
        assert node["fallback_policy"] in {
            "original_input",
            "deterministic",
            "rrf",
            "explicit_failure",
            "skip",
        }
        assert node["model_purpose"] in {"embedding", "reranker", "chat", "asr"}
        assert node["default"] in {"local", "api", "off"}


def test_prompt_profiles_are_versioned_and_stable_for_generative_nodes():
    for node in NODES:
        profiles = profiles_for_node(node["id"])
        if node["kind"] == "generative":
            assert profiles, node["id"]
        else:
            assert profiles == ()
        for profile in profiles:
            assert profile.status in {"draft", "production"}
            assert len(profile.template_hash) == 16
            if profile.status == "production":
                assert profile.template_refs
                if profile.output_schema.get("type") == "object":
                    assert profile.output_schema.get("properties")
                    assert profile.output_schema.get("required")
            assert profile.public_summary() == profile.public_summary()


def test_production_prompt_hash_changes_with_real_template(monkeypatch):
    import emotion.emotion_llm as emotion_module

    profile = next(
        item for item in profiles_for_node("emotion_llm")
        if item.prompt_id == "emotion.analyze"
    )
    original_hash = profile.template_hash
    monkeypatch.setattr(
        emotion_module, "_SYSTEM_PROMPT", emotion_module._SYSTEM_PROMPT + "\n新增约束"
    )
    assert profile.template_hash != original_hash


def test_prompt_profile_repository_stage_promote_and_rollback(tmp_path):
    config = ConfigService(tmp_path / "webui_overrides.json")
    repository = PromptProfileRepository(config)
    v2 = repository.stage({
        "prompt_id": "emotion.analyze",
        "version": "2.0.0",
        "system_template": "system-v2",
        "user_template": "{input}",
        "variables": {"input": {"required": True}},
        "output_schema": {
            "type": "object", "required": ["primary"],
            "properties": {"primary": {"type": "string"}},
        },
    })
    promoted_v2 = repository.promote("emotion.analyze", ab_report={
        "candidate": {"schema_rate": 1.0, "golden_rate": 1.0,
                      "violation_count": 0}, "regressions": []})
    assert v2["status"] == "staging"
    assert promoted_v2["status"] == "production"

    repository.stage({
        "prompt_id": "emotion.analyze",
        "version": "3.0.0",
        "system_template": "system-v3",
        "user_template": "{input}",
        "output_schema": {
            "type": "object", "required": ["primary"],
            "properties": {"primary": {"type": "string"}},
        },
    })
    repository.promote("emotion.analyze", ab_report={
        "candidate": {"schema_rate": 1.0, "golden_rate": 1.0,
                      "violation_count": 0}, "regressions": []})
    rolled_back = repository.rollback("emotion.analyze")
    assert rolled_back["version"] == "2.0.0"
    assert config.get("prompt_profiles.production.emotion.analyze.version") == "2.0.0"


def test_prompt_promote_failure_keeps_old_production_and_staging(tmp_path):
    class FailingConfig(ConfigService):
        fail_next = False

        def _save(self):
            if self.fail_next:
                self.fail_next = False
                raise OSError("disk failed")
            super()._save()

    config = FailingConfig(tmp_path / "overrides.json")
    repository = PromptProfileRepository(config)
    schema = {
        "type": "object", "required": ["primary"],
        "properties": {"primary": {"type": "string"}},
    }
    repository.stage({
        "prompt_id": "emotion.analyze", "version": "1.0.0",
        "system_template": "v1", "user_template": "{input}",
        "variables": {"input": {"required": True}}, "output_schema": schema,
    })
    repository.promote("emotion.analyze", ab_report={
        "candidate": {"schema_rate": 1.0, "golden_rate": 1.0,
                      "violation_count": 0}, "regressions": []})
    staged = repository.stage({
        "prompt_id": "emotion.analyze", "version": "2.0.0",
        "system_template": "v2", "user_template": "{input}",
        "variables": {"input": {"required": True}}, "output_schema": schema,
    })
    config.fail_next = True

    with pytest.raises(OSError, match="disk failed"):
        repository.promote("emotion.analyze", ab_report={
        "candidate": {"schema_rate": 1.0, "golden_rate": 1.0,
                      "violation_count": 0}, "regressions": []})

    assert config.get("prompt_profiles.production.emotion.analyze.version") == "1.0.0"
    assert config.get("prompt_profiles.staging.emotion.analyze") == staged


def test_config_service_defaults_cover_all_functional_nodes(tmp_path):
    config = ConfigService(tmp_path / "webui_overrides.json")
    defaults = config.get("local_deploy.nodes")

    assert set(defaults) == EXPECTED_NODE_IDS
    assert all(defaults[node["id"]] == node["default"] for node in NODES)
    assert config.get("local_deploy.schema_version") == 1


def test_set_backend_persists_backend_and_model_in_one_batch(tmp_path):
    class RecordingConfig(ConfigService):
        def __init__(self, path):
            super().__init__(path)
            self.batch_calls = []

        def set_many(self, updates):
            self.batch_calls.append(dict(updates))
            super().set_many(updates)

    config = RecordingConfig(tmp_path / "webui_overrides.json")
    set_backend(config, "query_transform", "local", local_model="chat-small")

    assert config.batch_calls == [{
        "local_deploy.nodes.query_transform": "local",
        "local_deploy.node_models.query_transform": "chat-small",
    }]


def test_node_update_rolls_back_config_when_runtime_apply_fails(
    tmp_path, monkeypatch,
):
    from core import j_space_bootstrap
    from web import config_service as config_service_module
    from web.routers import jspace

    j_space_bootstrap._intent_decomposer = IntentDecomposer()
    config = ConfigService(tmp_path / "webui_overrides.json")
    set_backend(config, "intent_decomposition", "off")
    monkeypatch.setattr(config_service_module, "_instance", config)

    real_set_backend = jspace.set_intent_backend

    def fail_api_backend(backend, local_model=None):
        if backend == "api":
            raise RuntimeError("apply failed")
        real_set_backend(backend, local_model)

    monkeypatch.setattr(jspace, "set_intent_backend", fail_api_backend)

    app = FastAPI()
    app.include_router(local_deploy_router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.state.core = SimpleNamespace(_vec_store=None)
    with TestClient(app) as client:
        response = client.put("/local-deploy/model-nodes", json={
            "node_id": "intent_decomposition", "backend": "api"
        })

    assert response.status_code == 409
    assert config.get("local_deploy.nodes.intent_decomposition") == "off"


@pytest.mark.asyncio
async def test_shared_local_model_reference_prevents_stop(tmp_path):
    config = ConfigService(tmp_path / "webui_overrides.json")
    set_backend(config, "query_transform", "local", local_model="chat-small")
    set_backend(config, "portrait", "local", local_model="chat-small")

    assert await model_has_other_local_references(
        config, "query_transform", "chat-small"
    ) is True


def test_reranker_runtime_adapter_accepts_common_backend_signature():
    service = LocalRerankerService(None, fallback=None)
    service.set_backend("off", "ignored-model-id")
    assert service._backend == "off"


def test_intent_decomposition_runtime_adapter_is_wired():
    from core import j_space_bootstrap

    j_space_bootstrap._intent_decomposer = IntentDecomposer()
    apply_to_runtime(
        SimpleNamespace(), None, "intent_decomposition", "off", local_model=None
    )

    assert j_space_bootstrap.get_intent_decomposer().use_llm is False


@pytest.mark.asyncio
async def test_asr_local_selection_is_rejected_until_runtime_exists():
    node = next(node for node in NODES if node["id"] == "asr")
    with pytest.raises(ValueError, match="ASR.*本地"):
        await validate_local_selection(
            core=SimpleNamespace(local_ai_instances=None),
            node=node,
            local_model="whisper-local",
        )
