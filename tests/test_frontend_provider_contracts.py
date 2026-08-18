import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_provider_store_owns_lifecycle_and_draft_fingerprint():
    text = source("web/frontend/src/stores/providers.ts")
    for action in (
        "loadProviders",
        "loadCapabilities",
        "discoverModels",
        "testDraft",
        "createProvider",
        "updateProvider",
        "deleteProvider",
    ):
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
    assert "Object.entries(modelValue.headers)" in editor
    assert "addHeader" in editor
    assert "removeHeader" in editor


def test_provider_components_do_not_import_raw_http_helpers():
    for path in (
        "web/frontend/src/components/models/CapabilityMatrix.vue",
        "web/frontend/src/components/models/CustomMappingEditor.vue",
        "web/frontend/src/components/models/ProviderWizard.vue",
    ):
        text = source(path)
        assert "from '../../api'" not in text
        assert "from \"../../api\"" not in text


def test_wizard_and_models_view_use_unified_lifecycle():
    wizard = source("web/frontend/src/components/models/ProviderWizard.vue")
    view = source("web/frontend/src/views/ModelsView.vue")
    for step in ("protocol", "connection", "verification", "review"):
        assert step in wizard
    for contract in ("CapabilityMatrix", "CustomMappingEditor", "canSave", "testDraft", "manualModel"):
        assert contract in wizard
    assert "useProvidersStore" in view
    assert "ProviderWizard" in view
    assert "flush: 'sync'" in wizard
    assert "providersStore.loadCapabilities" in view
    for legacy in ("/models/providers", "/models/providers/reorder", "/health/test/llm"):
        assert legacy not in view
    assert ".drag-handle" not in view


@pytest.mark.skipif(sys.platform == "win32", reason="esbuild binary path differs on Windows (.cmd)")
def test_provider_store_binds_test_results_to_normalized_request_snapshots(tmp_path):
    entry = tmp_path / "provider-store-snapshot.ts"
    bundle = tmp_path / "provider-store-snapshot.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ providerApi, fingerprintProviderDraft }} = await import({str(ROOT / 'web/frontend/src/api/providers.ts')!r})
            const {{ useProvidersStore }} = await import({str(ROOT / 'web/frontend/src/stores/providers.ts')!r})

            const reports = []
            const requests = []
            providerApi.test = (draft, credentials) => {{
              requests.push({{ draft, credentials }})
              return new Promise(resolve => reports.push(resolve))
            }}
            const makeDraft = (model) => ({{
              id: 'custom', label: 'Custom', protocol: 'custom-map', base_url: 'https://example.test/v1',
              chat_path: '/chat', models_path: '/models', default_model: model, enabled: true,
              auth: {{ required: true, header: 'Authorization', scheme: 'Bearer' }},
              capabilities: {{ tools: true, vision: false, streaming: true, model_discovery: true, json_mode: false }},
              headers: {{ 'X-Second': '2', 'X-First': '1' }},
              mapping: {{ request: {{ model: 'input.model', messages: 'input.messages' }}, response: {{ text: 'result.text' }}, stream: {{ text: 'delta.text' }}, models: 'data.*.id' }},
            }})
            setActivePinia(createPinia())
            const store = useProvidersStore()
            const draft = makeDraft('old-model')
            const oldFingerprint = fingerprintProviderDraft(draft)
            const oldRequest = store.testDraft(draft, {{ api_key: 'secret' }})
            draft.default_model = 'new-model'
            draft.headers['X-First'] = 'changed'
            if (requests[0].draft.default_model !== 'old-model' || requests[0].draft.headers['X-First'] !== '1') {{
              throw new Error('请求未使用深拷贝快照')
            }}
            const newFingerprint = fingerprintProviderDraft(draft)
            const newRequest = store.testDraft(draft, {{ api_key: 'secret' }})
            reports[1]({{ available: true, capabilities: draft.capabilities, models: ['new-model'], error: null }})
            await newRequest
            reports[0]({{ available: true, capabilities: requests[0].draft.capabilities, models: ['old-model'], error: null }})
            await oldRequest
            if (store.testedFingerprint !== newFingerprint || store.testedFingerprint === oldFingerprint) {{
              throw new Error('旧响应覆盖了最新测试 fingerprint')
            }}
            if (store.testReport?.models[0] !== 'new-model' || !store.canSave(draft, {{ api_key: 'secret' }})) {{
              throw new Error('最新响应未绑定到请求时快照')
            }}
            if (store.canSave(draft, {{ api_key: 'changed' }}) || store.canSave(draft, undefined)) {{
              throw new Error('canSave 未精确比较凭证摘要')
            }}
            const emptyCredentialRequest = store.testDraft(draft, undefined)
            reports[2]({{ available: true, capabilities: draft.capabilities, models: ['new-model'], error: null }})
            await emptyCredentialRequest
            if (!store.canSave(draft, undefined) || !store.canSave(draft, {{ api_key: '' }})) {{
              throw new Error('空凭证与 undefined 凭证摘要不稳定')
            }}
            const invalidatedRequest = store.testDraft(draft, undefined)
            store.invalidateTest()
            reports[3]({{ available: true, capabilities: draft.capabilities, models: ['stale-model'], error: null }})
            await invalidatedRequest
            if (store.testReport !== null || store.testedFingerprint !== '') {{
              throw new Error('显式失效后，在途测试响应仍覆盖了失效状态')
            }}
            providerApi.create = async () => draft
            providerApi.update = async () => draft
            let createRejected = false
            let updateRejected = false
            try {{ await store.createProvider(draft, {{ api_key: 'changed' }}) }} catch {{ createRejected = true }}
            try {{ await store.updateProvider('custom', draft, {{ api_key: 'changed' }}) }} catch {{ updateRejected = true }}
            if (!createRejected || !updateRejected) {{
              throw new Error('create/update 未精确拒绝与测试时不同的凭证')
            }}
            if (JSON.stringify(store.$state).includes('secret')) {{
              throw new Error('store 保存了原始 key')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [str(frontend / "node_modules/.bin/esbuild"), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_credential_summary_stays_private_and_save_checks_include_credentials():
    api = source("web/frontend/src/api/providers.ts")
    store = source("web/frontend/src/stores/providers.ts")
    wizard = source("web/frontend/src/components/models/ProviderWizard.vue")
    assert "export function summarizeCredentials" not in api
    assert "export function summarizeCredentials" not in store
    assert "summarizeCredentials" not in store.split("return {", 1)[1]
    assert "canSave(draft, credentials)" in store
    assert store.count("if (!canSave(draft, credentials))") == 2
    assert wizard.count("providers.canSave(draft, credentials)") == 2
