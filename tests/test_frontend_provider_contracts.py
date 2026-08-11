from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_provider_store_owns_lifecycle_and_draft_fingerprint():
    text = source("web/frontend/src/stores/providers.ts")
    for action in ("loadProviders", "testDraft", "createProvider", "updateProvider", "deleteProvider"):
        assert action in text
    for contract in ("testedFingerprint", "fingerprintProviderDraft", "invalidateTest", "canSave"):
        assert contract in text


def test_provider_api_uses_canonical_endpoints_and_protocols():
    text = source("web/frontend/src/api/providers.ts")
    for value in ("openai", "anthropic", "ollama", "custom-map"):
        assert value in text
    for path in ('"/providers"', '"/providers/test"', '"/capabilities"', '"/models"'):
        assert path in text


def test_capability_and_mapping_components_are_structured():
    matrix = source("web/frontend/src/components/models/CapabilityMatrix.vue")
    editor = source("web/frontend/src/components/models/CustomMappingEditor.vue")
    for field in ("tools", "vision", "streaming", "model_discovery", "json_mode"):
        assert field in matrix
    for field in ("chat_path", "models_path", "request", "response", "stream", "headers"):
        assert field in editor
    assert "eval(" not in editor
    assert "new Function" not in editor


def test_wizard_and_models_view_use_unified_lifecycle():
    wizard = source("web/frontend/src/components/models/ProviderWizard.vue")
    view = source("web/frontend/src/views/ModelsView.vue")
    for step in ("protocol", "connection", "verification", "review"):
        assert step in wizard
    for contract in ("CapabilityMatrix", "CustomMappingEditor", "canSave", "testDraft", "manualModel"):
        assert contract in wizard
    assert "useProvidersStore" in view
    assert "ProviderWizard" in view
    for legacy in ("/models/providers", "/models/providers/reorder"):
        assert legacy not in view
