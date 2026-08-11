# Unified Provider Onboarding UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a four-step, test-before-save Provider onboarding flow in ModelsView with real OpenAI-compatible, Anthropic, Ollama, and custom-mapping backend support.

**Architecture:** Complete the canonical ProviderService contract first, then expose it through stable HTTP responses. Add a typed frontend API and Pinia store as the only lifecycle state owner, compose the wizard from focused components, and replace only the legacy Provider section in ModelsView.

**Tech Stack:** Python 3.11, FastAPI, httpx, pytest, Vue 3, TypeScript, Pinia, Naive UI, Vite

## Global Constraints

- Keep ModelsView as the only navigation entry; do not add a sidebar route.
- Support frontend protocol values `openai`, `anthropic`, `ollama`, and `custom-map`.
- Normalize protocols to `openai_compatible`, `anthropic`, `ollama`, and `custom_mapping` on the backend.
- Do not expose credentials in list, capability, model, or error responses.
- Do not allow executable custom templates, arbitrary interpolation, absolute endpoint paths, or unrestricted mapping paths.
- Require a successful test of the exact current draft before frontend save.
- Repeat backend staging before persistence and roll back all partial state on failure.
- Do not silently fall back to another provider.
- Do not preserve drag ordering without a canonical API contract.

---

### Task 1: Complete Custom Provider Backend Contract

**Files:**
- Modify: `llm_gateway/provider_service.py`
- Modify: `llm_gateway/contracts.py`
- Modify: `llm_gateway/transports/__init__.py`
- Test: `tests/test_provider_onboarding.py`
- Test: `tests/test_provider_transports.py`

**Interfaces:**
- Consumes: `CustomMappingTransport(base_url, mapping=..., headers=..., api_key=..., capabilities=..., default_model=..., chat_path=..., models_path=...)`.
- Produces: `ProviderService.test/create/update()` support for `openai`, `anthropic`, `ollama`, and `custom-map` drafts; persisted custom mapping metadata; credential-optional authentication definitions.

- [ ] **Step 1: Add failing backend tests**

```python
def test_provider_service_normalizes_custom_map_and_restores_mapping(provider_service):
    draft = {
        "id": "mapped",
        "protocol": "custom-map",
        "base_url": "https://example.test/v1",
        "default_model": "mapped-chat",
        "chat_path": "/generate",
        "models_path": "/catalog",
        "auth": {"required": False, "header": "X-Key", "scheme": ""},
        "headers": {"X-Key": "{api_key}"},
        "mapping": {
            "request": {"messages": "input.messages", "model": "input.model"},
            "response": {"text": "result.text"},
            "stream": {"text": "delta.text"},
            "models": "data.*.id",
        },
    }
    definition = provider_service._definition(draft)
    assert definition.protocol.value == "custom_mapping"
    assert definition.metadata["mapping"]["response"]["text"] == "result.text"
    assert provider_service._record(definition)["chat_path"] == "/generate"


def test_custom_mapping_factory_receives_persisted_contract(provider_service):
    definition = provider_service._definition(custom_mapping_draft())
    transport = provider_service._build_transport(definition, "secret")
    assert transport.__class__.__name__ == "CustomMappingTransport"
```

- [ ] **Step 2: Verify the tests fail**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_transports.py -q`
Expected: FAIL because `custom-map` is not normalized or constructed by ProviderService.

- [ ] **Step 3: Implement canonical metadata and transport construction**

```python
_PROTOCOL_ALIASES = {
    "openai": ProviderProtocol.OPENAI_COMPATIBLE.value,
    "custom-map": ProviderProtocol.CUSTOM_MAPPING.value,
}

protocol_value = _PROTOCOL_ALIASES.get(str(draft.get("protocol", "")), str(draft.get("protocol", "")))
protocol = ProviderProtocol(protocol_value)
metadata = {
    "label": str(draft.get("label", draft["id"])),
    "enabled": bool(draft.get("enabled", True)),
    "headers": dict(draft.get("headers") or {}),
    "mapping": dict(draft.get("mapping") or {}),
}
```

Construct `CustomMappingTransport` from `definition.metadata`, `definition.endpoint`, `definition.auth`, and `definition.capabilities`. Preserve those values in `_record()` and `_restore_custom_definitions()`.

- [ ] **Step 4: Run backend tests**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_transports.py -q`
Expected: PASS.

- [ ] **Step 5: Review Task 1 diff**

Run: `git diff --check -- llm_gateway tests/test_provider_onboarding.py tests/test_provider_transports.py`
Expected: exit 0 and no credential values in serialized records.

### Task 2: Harden Provider HTTP Boundary

**Files:**
- Modify: `web/routers/providers.py`
- Modify: `llm_gateway/provider_service.py`
- Test: `tests/test_provider_onboarding.py`

**Interfaces:**
- Consumes: ProviderService lifecycle methods from Task 1.
- Produces: stable `/api/v1/providers` JSON contracts and 400/404/409/422 error mapping.

- [ ] **Step 1: Add failing API boundary tests**

```python
def test_provider_api_maps_validation_and_conflicts_without_credentials(client):
    invalid = client.post("/api/v1/providers/test", json={"draft": {"id": "bad", "protocol": "custom-map", "base_url": "file:///tmp/model"}})
    assert invalid.status_code == 400
    duplicate = client.post("/api/v1/providers", json=valid_provider_payload("builtin-id"))
    assert duplicate.status_code in {400, 409}
    assert "api_key" not in duplicate.text


def test_provider_list_redacts_custom_mapping_credentials(client):
    response = client.get("/api/v1/providers")
    assert response.status_code == 200
    assert "secret-value" not in response.text
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py -q`
Expected: FAIL where `ValueError` or `KeyError` currently escapes as 500.

- [ ] **Step 3: Add explicit exception translation and safe serialization**

```python
def _validation_error(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


def _serialize(definition: ProviderDefinition) -> dict[str, Any]:
    return {
        "id": definition.id,
        "protocol": definition.protocol.value,
        "base_url": definition.endpoint.base_url,
        "chat_path": definition.endpoint.chat_path,
        "models_path": definition.endpoint.models_path,
        "default_model": definition.default_model,
        "builtin": definition.builtin,
        "capabilities": asdict(definition.capabilities),
        "auth": {"required": definition.auth.required, "header": definition.auth.header, "scheme": definition.auth.scheme},
        "mapping": dict(definition.metadata.get("mapping") or {}),
        "headers": dict(definition.metadata.get("headers") or {}),
    }
```

Map missing providers to 404, route references to 409, failed connection tests to 422, and malformed drafts to 400.

- [ ] **Step 4: Run API and security regression tests**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_catalog.py -q`
Expected: PASS.

- [ ] **Step 5: Review Task 2 diff**

Run: `git diff --check -- web/routers/providers.py llm_gateway/provider_service.py tests/test_provider_onboarding.py`
Expected: exit 0.

### Task 3: Add Typed Provider API and Pinia Store

**Files:**
- Create: `web/frontend/src/api/providers.ts`
- Create: `web/frontend/src/stores/providers.ts`
- Modify: `web/frontend/src/api/index.ts`
- Create: `tests/test_frontend_provider_contracts.py`

**Interfaces:**
- Produces: `providerApi`, `useProvidersStore`, `fingerprintProviderDraft`, `ProviderDraft`, `ProviderDefinition`, and `CapabilityReport`.
- Consumes: `/api/v1/providers` endpoints from Task 2.

- [ ] **Step 1: Add failing frontend contract tests**

```python
def test_provider_store_owns_lifecycle_and_draft_fingerprint():
    source = read_project_file("web/frontend/src/stores/providers.ts")
    for action in ("loadProviders", "testDraft", "createProvider", "updateProvider", "deleteProvider"):
        assert action in source
    assert "testedFingerprint" in source
    assert "fingerprintProviderDraft" in source
    assert "invalidateTest" in source


def test_provider_api_uses_canonical_endpoints():
    source = read_project_file("web/frontend/src/api/providers.ts")
    for path in ('"/providers"', '"/providers/test"', '"/capabilities"', '"/models"'):
        assert path in source
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q`
Expected: FAIL because the API and store files do not exist.

- [ ] **Step 3: Implement typed API and deterministic draft fingerprinting**

```typescript
export type ProviderProtocolInput = 'openai' | 'anthropic' | 'ollama' | 'custom-map'
export type ProviderProtocol = 'openai_compatible' | 'anthropic' | 'ollama' | 'custom_mapping'

export interface CapabilityReport {
  available: boolean
  capabilities: Record<'tools' | 'vision' | 'streaming' | 'model_discovery' | 'json_mode', boolean>
  models: string[]
  error: string | null
}

export function fingerprintProviderDraft(draft: ProviderDraft): string {
  return JSON.stringify(normalizeDraft(draft))
}
```

The normalized fingerprint must include protocol, URL, endpoint paths, auth definition, default model, capability toggles, headers, and mapping, while excluding transient UI fields.

- [ ] **Step 4: Implement store test invalidation and lifecycle actions**

```typescript
const testedFingerprint = ref('')
const testReport = ref<CapabilityReport | null>(null)

function invalidateTest() {
  testedFingerprint.value = ''
  testReport.value = null
}

function canSave(draft: ProviderDraft) {
  return testReport.value?.available === true && testedFingerprint.value === fingerprintProviderDraft(draft)
}
```

Each create/update action verifies `canSave(draft)` before calling the API and reloads providers after success.

- [ ] **Step 5: Run store contracts and TypeScript checking**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit`
Expected: PASS.

### Task 4: Build Capability and Custom Mapping Components

**Files:**
- Create: `web/frontend/src/components/models/CapabilityMatrix.vue`
- Create: `web/frontend/src/components/models/CustomMappingEditor.vue`
- Modify: `tests/test_frontend_provider_contracts.py`

**Interfaces:**
- Consumes: `CapabilityReport`, `ProviderDraft`, and structured custom mapping types from Task 3.
- Produces: `CapabilityMatrix` report presentation and `CustomMappingEditor` `v-model` updates.

- [ ] **Step 1: Add failing component contracts**

```python
def test_capability_matrix_lists_all_capabilities():
    source = read_project_file("web/frontend/src/components/models/CapabilityMatrix.vue")
    for field in ("tools", "vision", "streaming", "model_discovery", "json_mode"):
        assert field in source


def test_custom_mapping_editor_is_structured_and_safe():
    source = read_project_file("web/frontend/src/components/models/CustomMappingEditor.vue")
    for field in ("chat_path", "models_path", "request", "response", "stream", "headers"):
        assert field in source
    assert "eval(" not in source
    assert "new Function" not in source
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q`
Expected: FAIL because both components are absent.

- [ ] **Step 3: Implement focused Naive UI components**

```vue
<CapabilityMatrix :report="providers.testReport" />
<CustomMappingEditor v-if="draft.protocol === 'custom-map'" v-model="draft.custom" />
```

Use current card, form-item, input, tag, alert, and responsive grid patterns. Emit immutable copied mapping values instead of mutating props.

- [ ] **Step 4: Verify component contracts and types**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit`
Expected: PASS.

### Task 5: Build Four-Step Provider Wizard

**Files:**
- Create: `web/frontend/src/components/models/ProviderWizard.vue`
- Modify: `tests/test_frontend_provider_contracts.py`

**Interfaces:**
- Consumes: `useProvidersStore`, `CapabilityMatrix`, and `CustomMappingEditor`.
- Produces: `v-model:show`, optional `provider` edit input, and `saved` event.

- [ ] **Step 1: Add failing wizard contracts**

```python
def test_provider_wizard_exposes_protocols_and_four_steps():
    source = read_project_file("web/frontend/src/components/models/ProviderWizard.vue")
    for protocol in ("openai", "anthropic", "ollama", "custom-map"):
        assert protocol in source
    for step in ("protocol", "connection", "verification", "review"):
        assert step in source
    assert "CapabilityMatrix" in source
    assert "CustomMappingEditor" in source


def test_provider_wizard_requires_exact_tested_draft_before_save():
    source = read_project_file("web/frontend/src/components/models/ProviderWizard.vue")
    assert "canSave" in source
    assert "testDraft" in source
    assert "manualModel" in source
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q`
Expected: FAIL because `ProviderWizard.vue` is absent.

- [ ] **Step 3: Implement wizard state and credential hygiene**

```typescript
const step = ref<'protocol' | 'connection' | 'verification' | 'review'>('protocol')
const credentials = ref({ api_key: '' })
const manualModel = ref('')

watch(draft, () => providers.invalidateTest(), { deep: true })

function closeWizard() {
  credentials.value.api_key = ''
  providers.invalidateTest()
  emit('update:show', false)
}
```

Advance to review only after a successful test. Use discovered models when available; otherwise require explicit manual model input before save.

- [ ] **Step 4: Verify wizard contracts and types**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit`
Expected: PASS.

### Task 6: Migrate ModelsView Provider Lifecycle

**Files:**
- Modify: `web/frontend/src/views/ModelsView.vue`
- Modify: `tests/test_frontend_provider_contracts.py`
- Test: `tests/test_frontend_runtime_contracts.py`

**Interfaces:**
- Consumes: `ProviderWizard` and `useProvidersStore`.
- Preserves: task routing, generation parameters, credential-pool status, and usage charts.

- [ ] **Step 1: Add failing integration contracts**

```python
def test_models_view_uses_provider_store_and_wizard():
    source = read_project_file("web/frontend/src/views/ModelsView.vue")
    assert "useProvidersStore" in source
    assert "ProviderWizard" in source
    for legacy in ("/models/providers", "/health/test/llm", "/models/providers/reorder"):
        assert legacy not in source


def test_provider_components_do_not_call_raw_http():
    for name in ("ProviderWizard.vue", "CapabilityMatrix.vue", "CustomMappingEditor.vue"):
        source = read_project_file(f"web/frontend/src/components/models/{name}")
        assert "apiGet" not in source
        assert "apiPost" not in source
        assert "apiPut" not in source
        assert "apiDelete" not in source
```

- [ ] **Step 2: Verify integration test fails**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py tests/test_frontend_runtime_contracts.py -q`
Expected: FAIL because ModelsView still owns legacy CRUD and modal state.

- [ ] **Step 3: Replace only the provider section**

```typescript
const providersStore = useProvidersStore()
const providerWizardOpen = ref(false)
const editingProvider = ref<ProviderDefinition | null>(null)

onMounted(() => providersStore.loadProviders())
```

Render built-ins as immutable, custom providers with edit/delete actions, and `ProviderWizard` for create/edit. Remove drag ordering and legacy key/test modal calls. Keep route and chart state unchanged.

- [ ] **Step 4: Run frontend integration checks**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py tests/test_frontend_runtime_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit && npm run build`
Expected: PASS and production build exit 0.

### Task 7: Full Task 20 Verification and Review

**Files:**
- Modify: `.superpowers/sdd/progress.md`
- Create: `.superpowers/sdd/task-20-report.md`
- Review: all Task 20 changed files

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: reproducible test evidence, final review verdict, and synchronized progress state.

- [ ] **Step 1: Run the complete focused suite**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_catalog.py tests/test_provider_transports.py tests/test_frontend_provider_contracts.py tests/test_frontend_runtime_contracts.py tests/test_local_ai_api.py -q`
Expected: all tests pass with zero failures.

- [ ] **Step 2: Run frontend and static gates**

Run: `cd web/frontend && npx vue-tsc --noEmit && npm run build`
Expected: both commands exit 0.

Run: `cd /home/orangepi/ai-agent && git diff --check`
Expected: exit 0.

- [ ] **Step 3: Perform independent code review**

Review requirements:

- No credential exposure.
- No stale draft save.
- No legacy Provider lifecycle calls in ModelsView.
- Custom mapping reaches the real transport.
- Create/update rollback remains atomic.
- No critical or important findings remain unresolved.

- [ ] **Step 4: Record evidence and synchronize progress**

Write `.superpowers/sdd/task-20-report.md` with changed files, red/green evidence, exact command outputs, review findings, and remaining non-blocking limitations. Mark Task 20 complete in `.superpowers/sdd/progress.md` only after all gates and review pass.

- [ ] **Step 5: Commit only if explicitly requested**

Do not create a Git commit unless the user explicitly requests one. If requested, stage only Task 20 files and use:

```bash
git commit -m "feat(web): add unified model provider onboarding"
```
