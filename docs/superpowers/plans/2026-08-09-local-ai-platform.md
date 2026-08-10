# Local AI Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a maintainable local AI platform with real hardware discovery, a ModelScope-backed ONNX market, chat/embedding/reranker runtimes, and unified cloud/local/custom provider onboarding inside the existing Web UI.

**Architecture:** Introduce `local_ai` as the authoritative deep module for devices, model manifests, installations, downloads, runtimes, and instances. Keep existing APIs as compatibility facades while migrating VectorStore and ModelRouter. Consolidate provider metadata and lifecycle into a catalog consumed by setup, discovery, routing, and sub-agents.

**Tech Stack:** Python 3.11+, FastAPI, aiosqlite, httpx, ONNX Runtime, ONNX Runtime GenAI, Vue 3, TypeScript, Pinia, Naive UI, pytest.

## Global Constraints

- Preserve the existing Vue 3 Web UI visual system and the single Local Deployment sidebar entry.
- Local Deployment contains Deployments, Model Market, Installed Models, Compute Devices, and Download Tasks tabs.
- Chat uses ONNX Runtime GenAI; embedding and reranker use standard ONNX Runtime.
- Windows x64, Linux x64, and Linux ARM64 receive complete paths; Android receives contracts only.
- Curated downloads default to 5 GB or less; larger custom models require advanced confirmation.
- Non-bundled models require server-side destination selection; saving the default is optional.
- Market models are never bundled in release artifacts.
- No silent cross-provider fallback or automatic model switching.
- Credentials remain encrypted and never enter global process environment for new provider paths.
- Every production change follows red-green-refactor and preserves existing compatibility until callers migrate.
- Do not modify or discard the user's pre-existing uncommitted `cli.py` changes.

---

## Phase 1: Domain Foundation and Real Device Discovery

### Task 1: Local AI Domain Contracts

**Files:**
- Create: `local_ai/__init__.py`
- Create: `local_ai/contracts.py`
- Test: `tests/test_local_ai_contracts.py`

**Interfaces:**
- Produces: `ModelPurpose`, `RuntimeKind`, `TaskState`, `DeviceState`, `ComputeDevice`, `ExecutionBackend`, `CatalogModel`, `InstalledModel`, `DownloadTask`, `RuntimeProfile`, and `ModelInstance`.
- Serialization: every public record exposes `to_dict() -> dict[str, Any]` and `from_dict(data: Mapping[str, Any]) -> Self`.

- [ ] **Step 1: Write failing round-trip and validation tests**

```python
def test_compute_device_round_trip_preserves_backend_evidence():
    device = ComputeDevice(
        id="cpu:0", name="ARM Cortex-A76", kind="cpu", architecture="aarch64",
        state=DeviceState.AVAILABLE, memory_total=8_000_000_000,
        memory_available=6_000_000_000,
        backends=(ExecutionBackend(runtime=RuntimeKind.ORT, provider="CPUExecutionProvider", healthy=True, evidence={"probe": "session"}),),
        evidence={"source": "/proc/cpuinfo"},
    )
    assert ComputeDevice.from_dict(device.to_dict()) == device

def test_catalog_model_requires_immutable_revision_and_files():
    with pytest.raises(ValueError):
        CatalogModel(id="bad", source="modelscope", repository="owner/model", revision="", purpose=ModelPurpose.CHAT, files=())
```

- [ ] **Step 2: Verify the tests fail because contracts do not exist**

Run: `.venv/bin/python -m pytest tests/test_local_ai_contracts.py -q`
Expected: FAIL on import from `local_ai.contracts`.

- [ ] **Step 3: Implement immutable enums and dataclasses with explicit validation**

Implement JSON-safe enum values, tuples for immutable collections, UTC timestamps, path strings rather than `Path` in transport records, and reject negative sizes, empty IDs, mutable revisions, and incomplete catalog file manifests.

- [ ] **Step 4: Verify contract tests pass**

Run: `.venv/bin/python -m pytest tests/test_local_ai_contracts.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit the domain foundation**

```bash
git add local_ai tests/test_local_ai_contracts.py
git commit -m "feat(local-ai): add platform domain contracts"
```

### Task 2: Cross-Platform Hardware Probes

**Files:**
- Create: `local_ai/devices/__init__.py`
- Create: `local_ai/devices/system_probe.py`
- Create: `local_ai/devices/vip_probe.py`
- Test: `tests/test_local_ai_system_probe.py`
- Modify: `memory/npu_embed.py`

**Interfaces:**
- Consumes: `ComputeDevice`, `DeviceState`.
- Produces: `probe_system_devices(platform: str | None = None) -> list[ComputeDevice]`.
- Produces: `parse_vip_probe(payload: str) -> ComputeDevice | None` and `probe_vip_backend(...) -> ComputeDevice | None`.

- [ ] **Step 1: Write failing fixture-driven platform tests**

```python
def test_linux_arm_probe_uses_device_tree_model(monkeypatch):
    monkeypatch.setattr(system_probe, "_read_text", lambda path: "Orange Pi 5 Plus" if "device-tree/model" in str(path) else "")
    devices = system_probe.probe_system_devices("linux")
    assert any(d.architecture == "aarch64" and "Orange Pi" in d.name for d in devices)

def test_vip_probe_never_invents_model_or_tops():
    device = parse_vip_probe('{"available":true,"driver":"6.4.3"}')
    assert device is not None
    assert "VIP9000" not in device.name
    assert "tops" not in device.evidence
```

- [ ] **Step 2: Verify tests fail against missing probes**

Run: `.venv/bin/python -m pytest tests/test_local_ai_system_probe.py -q`
Expected: FAIL on missing functions.

- [ ] **Step 3: Implement evidence-based Windows/Linux probes and structured VIP parsing**

Use injectable command and file readers. Do not parse UI labels. Remove the fixed VIP9000/TOPS return path from `web/routers/local_deploy.py` only after the new registry consumes the probe.

- [ ] **Step 4: Verify probe tests and existing NPU tests**

Run: `.venv/bin/python -m pytest tests/test_local_ai_system_probe.py tests/test_npu_embed.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit hardware probes**

```bash
git add local_ai/devices memory/npu_embed.py tests/test_local_ai_system_probe.py
git commit -m "feat(local-ai): discover hardware from runtime evidence"
```

### Task 3: ONNX Execution Provider Registry

**Files:**
- Create: `local_ai/devices/ort_providers.py`
- Create: `local_ai/devices/registry.py`
- Test: `tests/test_local_ai_device_registry.py`
- Modify: `memory/local_embed.py`

**Interfaces:**
- Produces: `OrtProviderProbe.list_available() -> tuple[str, ...]`.
- Produces: `OrtProviderProbe.verify(provider: str) -> ExecutionBackend`.
- Produces: `DeviceRegistry.scan(force: bool = False) -> list[ComputeDevice]`.
- Produces: `DeviceRegistry.recommend(model: CatalogModel, override: str | None = None) -> RuntimeProfile`.

- [ ] **Step 1: Write failing provider health and recommendation tests**

```python
def test_unhealthy_available_provider_is_not_selectable(fake_ort):
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    fake_ort.fail_session_for = {"ROCMExecutionProvider"}
    registry = DeviceRegistry(ort_module=fake_ort)
    devices = registry.scan(force=True)
    assert registry.backend("ROCMExecutionProvider").healthy is False
    assert registry.backend("CPUExecutionProvider").healthy is True

def test_manual_override_must_be_model_compatible(registry, chat_manifest):
    with pytest.raises(IncompatibleBackendError):
        registry.recommend(chat_manifest, override="vip:0")
```

- [ ] **Step 2: Verify the tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_device_registry.py -q`
Expected: FAIL because registry is absent.

- [ ] **Step 3: Implement minimal-session verification, cache, rescan, compatibility filtering, and recommendation**

`LocalEmbeddingProvider` accepts an ordered `providers: list[str]` and `provider_options: list[dict[str, Any]] | None` supplied by the registry; remove its fixed CPU list.

- [ ] **Step 4: Verify registry and embedding adapter tests**

Run: `.venv/bin/python -m pytest tests/test_local_ai_device_registry.py tests/test_local_embed_mode.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit runtime provider discovery**

```bash
git add local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
git commit -m "feat(local-ai): verify and select ONNX execution providers"
```

## Phase 2: Catalog, Storage, and Installation

### Task 4: Versioned Curated Catalog

**Files:**
- Create: `local_ai/catalog/__init__.py`
- Create: `local_ai/catalog/schema.py`
- Create: `local_ai/catalog/curated.py`
- Create: `config/local_model_catalog.json`
- Test: `tests/test_local_ai_catalog.py`

**Interfaces:**
- Produces: `CatalogLoader.load_curated() -> list[CatalogModel]`.
- Produces: `CatalogLoader.filter(purpose: ModelPurpose | None, max_download_bytes: int | None, advanced: bool) -> list[CatalogModel]`.

- [ ] **Step 1: Write failing schema and 5 GB filter tests**

```python
def test_curated_entries_are_immutable_and_verifiable(loader):
    for model in loader.load_curated():
        assert model.revision not in {"main", "master", "latest"}
        assert model.files
        assert all(f.sha256 and f.size > 0 for f in model.files)

def test_default_market_hides_models_over_five_gib(loader):
    assert all(m.download_size <= 5 * 1024**3 for m in loader.filter(None, None, advanced=False))
```

- [ ] **Step 2: Verify catalog tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_catalog.py -q`
Expected: FAIL because the loader does not exist.

- [ ] **Step 3: Implement strict JSON schema parsing and a small verified starter catalog**

Only include entries whose exact ModelScope revision, files, license, and runtime schema can be verified. Do not fabricate hashes or compatibility. If live source evidence is unavailable, ship an empty catalog with a documented remote-catalog URL seam rather than fake models.

- [ ] **Step 4: Verify catalog tests pass**

Run: `.venv/bin/python -m pytest tests/test_local_ai_catalog.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit the curated catalog**

```bash
git add local_ai/catalog config/local_model_catalog.json tests/test_local_ai_catalog.py
git commit -m "feat(local-ai): add verified local model catalog"
```

### Task 5: ModelScope Repository Adapter

**Files:**
- Create: `local_ai/catalog/modelscope.py`
- Test: `tests/test_local_ai_modelscope.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `ModelScopeRepository.list_files(repository: str, revision: str, token: str | None) -> list[RemoteFile]`.
- Produces: `ModelScopeRepository.inspect(...) -> CatalogInspection`.
- Uses `httpx`; do not require the full ModelScope SDK unless primary API evidence proves it necessary.

- [ ] **Step 1: Write failing mocked API tests for revision, pagination, auth, and compatibility**

```python
@pytest.mark.asyncio
async def test_custom_repository_requires_immutable_revision(repo):
    with pytest.raises(InvalidRevisionError):
        await repo.inspect("owner/model", "main", None)

@pytest.mark.asyncio
async def test_unknown_onnx_layout_is_saved_as_requires_configuration(repo, mock_transport):
    inspection = await repo.inspect("owner/custom", "abc123", None)
    assert inspection.runnable is False
    assert inspection.state == "requires_configuration"
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_modelscope.py -q`
Expected: FAIL on missing adapter.

- [ ] **Step 3: Implement SSRF-safe ModelScope file listing and recognized-layout inspection**

Recognize ORT GenAI `genai_config.json`, standard embedding manifests, and reranker manifests. Return evidence and missing requirements; do not guess purpose from repository name alone.

- [ ] **Step 4: Verify adapter tests pass**

Run: `.venv/bin/python -m pytest tests/test_local_ai_modelscope.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit ModelScope integration**

```bash
git add local_ai/catalog/modelscope.py tests/test_local_ai_modelscope.py pyproject.toml
git commit -m "feat(local-ai): inspect ModelScope ONNX repositories"
```

### Task 6: Server Storage Picker and Policy

**Files:**
- Create: `local_ai/models/storage.py`
- Create: `web/routers/local_ai_storage.py`
- Test: `tests/test_local_ai_storage.py`
- Modify: `web/server.py`
- Modify: `web/config_service.py`

**Interfaces:**
- Produces: `StoragePolicy.list_directory(path: str | None) -> DirectoryListing`.
- Produces: `StoragePolicy.validate_destination(path: str, required_bytes: int) -> StorageValidation`.
- API: `GET /api/v1/local-ai/storage`, `POST /api/v1/local-ai/storage/validate`, `GET/PUT /api/v1/local-ai/storage/default`.

- [ ] **Step 1: Write failing path, permission, free-space, and default behavior tests**

```python
def test_unsaved_destination_is_not_reused(config_service, policy, tmp_path):
    policy.validate_destination(str(tmp_path), 1024)
    assert config_service.get("local_ai.default_model_root", "") == ""

def test_saved_default_is_revalidated_before_download(config_service, policy, tmp_path):
    config_service.set("local_ai.default_model_root", str(tmp_path))
    assert policy.validate_destination(str(tmp_path), 1024).writable
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_storage.py -q`
Expected: FAIL because storage policy and routes are missing.

- [ ] **Step 3: Implement server-side browsing, validation, and optional default persistence**

Block traversal aliases, device files, and paths outside configured roots when restricted mode is active. Require enough space for downloads, partials, and atomic installation.

- [ ] **Step 4: Verify storage and API auth tests**

Run: `.venv/bin/python -m pytest tests/test_local_ai_storage.py tests/test_web_auth_enforcement.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit storage selection**

```bash
git add local_ai/models/storage.py web/routers/local_ai_storage.py web/server.py web/config_service.py tests/test_local_ai_storage.py
git commit -m "feat(local-ai): add secure server model directory selection"
```

### Task 7: Persistent Model Registry

**Files:**
- Create: `local_ai/models/registry.py`
- Create: `db/db_local_ai.py`
- Test: `tests/test_local_ai_model_registry.py`
- Modify: `db/database.py`

**Interfaces:**
- Produces: `ModelRegistry.list()`, `get(model_id)`, `register(installed)`, `mark_validation(...)`, and `remove(model_id)`.
- Produces: built-in BGE entry with `ownership="bundled"` and `removable=False`.

- [ ] **Step 1: Write failing migration, bundled-entry, ownership, and path-collision tests**

```python
@pytest.mark.asyncio
async def test_bundled_bge_is_registered_but_not_removable(registry):
    model = await registry.get("builtin:bge-small-zh-v1.5")
    assert model.ownership == "bundled"
    with pytest.raises(ModelRemovalBlockedError):
        await registry.remove(model.id)
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_model_registry.py -q`
Expected: FAIL on missing DB module.

- [ ] **Step 3: Add monotonic SQLite migration and registry transaction boundaries**

Store manifests as versioned JSON plus indexed identity, path, purpose, state, and timestamps. Use `DatabaseManager.write_transaction()` for multi-statement changes.

- [ ] **Step 4: Verify registry and full schema tests**

Run: `.venv/bin/python -m pytest tests/test_local_ai_model_registry.py tests/test_kg_v2_schema.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit model registry**

```bash
git add local_ai/models/registry.py db/db_local_ai.py db/database.py tests/test_local_ai_model_registry.py
git commit -m "feat(local-ai): persist installed model registry"
```

### Task 8: Resumable Download Manager

**Files:**
- Create: `local_ai/downloads/__init__.py`
- Create: `local_ai/downloads/manager.py`
- Create: `local_ai/downloads/transport.py`
- Create: `local_ai/downloads/verifier.py`
- Test: `tests/test_local_ai_downloads.py`

**Interfaces:**
- Produces: `DownloadManager.create(model, destination) -> DownloadTask`.
- Produces: `start(task_id)`, `pause(task_id)`, `resume(task_id)`, `cancel(task_id, discard_partials=False)`, and `recover()`.
- Emits: `local_ai_download_updated` events with the full `DownloadTask` payload.

- [ ] **Step 1: Write failing Range, progress, pause, cancel, restart, and hash tests**

```python
@pytest.mark.asyncio
async def test_resume_uses_range_from_existing_part(manager, http_server, destination):
    part = destination / "model.onnx.part"
    part.write_bytes(b"first")
    await manager.start("task-1")
    assert http_server.last_headers["Range"] == "bytes=5-"

@pytest.mark.asyncio
async def test_hash_mismatch_never_registers_model(manager, registry):
    task = await manager.start("bad-hash")
    assert task.state == TaskState.QUARANTINED
    assert await registry.get(task.model_id) is None
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_downloads.py -q`
Expected: FAIL because the manager is absent.

- [ ] **Step 3: Implement destination-local partials, Range negotiation, checkpoints, SHA256, quarantine, and atomic moves**

Use `CancelToken` checkpoints and monotonic byte accounting. A server returning 200 to a Range request restarts the file safely instead of appending duplicate bytes.

- [ ] **Step 4: Verify download tests pass**

Run: `.venv/bin/python -m pytest tests/test_local_ai_downloads.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit download lifecycle**

```bash
git add local_ai/downloads tests/test_local_ai_downloads.py
git commit -m "feat(local-ai): add resumable verified model downloads"
```

## Phase 3: Runtime and Agent Integration

### Task 9: Standard ORT Embedding and Reranker Runtimes

**Files:**
- Create: `local_ai/runtimes/__init__.py`
- Create: `local_ai/runtimes/base.py`
- Create: `local_ai/runtimes/ort_embedding.py`
- Create: `local_ai/runtimes/ort_reranker.py`
- Test: `tests/test_local_ai_ort_runtimes.py`

**Interfaces:**
- Produces: `EmbeddingRuntime.start(profile)`, `embed(texts) -> list[list[float]]`, `stop()`.
- Produces: `RerankerRuntime.start(profile)`, `score(query, documents) -> list[float]`, `stop()`.

- [ ] **Step 1: Write failing fake-session tests for dimensions, pooling, batching, and score shape**

```python
def test_embedding_rejects_manifest_dimension_mismatch(runtime):
    runtime.session.output = np.zeros((1, 384), dtype=np.float32)
    with pytest.raises(RuntimeValidationError):
        runtime.embed(["test"], expected_dimensions=512)

def test_reranker_preserves_document_order(runtime):
    assert runtime.score("q", ["a", "b"]) == [0.8, 0.2]
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_ort_runtimes.py -q`
Expected: FAIL on missing runtimes.

- [ ] **Step 3: Implement manifest-driven sessions with provider options**

Reuse tokenizer and normalization behavior from `memory/local_embed.py`, then leave that class as a compatibility adapter until VectorStore migrates.

- [ ] **Step 4: Verify runtime tests pass**

Run: `.venv/bin/python -m pytest tests/test_local_ai_ort_runtimes.py tests/test_local_embed_mode.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit standard ORT runtimes**

```bash
git add local_ai/runtimes tests/test_local_ai_ort_runtimes.py
git commit -m "feat(local-ai): add embedding and reranker ONNX runtimes"
```

### Task 10: ONNX Runtime GenAI Chat Runtime

**Files:**
- Create: `local_ai/runtimes/ort_genai.py`
- Test: `tests/test_local_ai_ort_genai.py`
- Modify: `pyproject.toml`
- Modify: `xiaoda-agent.spec`

**Interfaces:**
- Produces: `OrtGenAiChatRuntime.start(profile)`, `stream(messages, options, cancel_token) -> AsyncIterator[str]`, `health()`, and `stop()`.

- [ ] **Step 1: Write failing adapter-contract tests using a fake `onnxruntime_genai` module**

```python
@pytest.mark.asyncio
async def test_stream_yields_tokens_and_honors_cancel(runtime, cancel_token):
    chunks = []
    async for chunk in runtime.stream([{"role": "user", "content": "hi"}], {}, cancel_token):
        chunks.append(chunk)
        cancel_token.cancel()
    assert chunks == ["你"]
    assert runtime.generator_closed
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_ort_genai.py -q`
Expected: FAIL because adapter is absent.

- [ ] **Step 3: Implement lazy optional dependency loading and manifest-driven generation**

Do not import ORT GenAI at module import time. Normalize prompt templates, sampling options, cancellation, token decoding, and generator cleanup. Return a structured dependency error when a platform wheel is unavailable.

- [ ] **Step 4: Verify adapter and PyInstaller contract tests**

Run: `.venv/bin/python -m pytest tests/test_local_ai_ort_genai.py tests/test_windows_package_reliability.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit chat runtime**

```bash
git add local_ai/runtimes/ort_genai.py tests/test_local_ai_ort_genai.py pyproject.toml xiaoda-agent.spec
git commit -m "feat(local-ai): run local chat models with ORT GenAI"
```

### Task 11: Instance Manager and Runtime Registry

**Files:**
- Create: `local_ai/runtimes/registry.py`
- Create: `local_ai/instances/__init__.py`
- Create: `local_ai/instances/manager.py`
- Test: `tests/test_local_ai_instances.py`

**Interfaces:**
- Produces: `RuntimeRegistry.create(profile) -> RuntimeAdapter`.
- Produces: `InstanceManager.start(model_id, backend_override=None) -> ModelInstance`, `stop(instance_id)`, `list()`, `shutdown()`.

- [ ] **Step 1: Write failing lifecycle, serialization, device-loss, and shutdown-order tests**

```python
@pytest.mark.asyncio
async def test_device_loss_marks_instance_degraded(manager, device_registry):
    instance = await manager.start("local:chat")
    device_registry.remove(instance.device_id)
    await manager.refresh_health()
    assert manager.get(instance.id).state == "degraded"

@pytest.mark.asyncio
async def test_shutdown_stops_all_runtimes_before_database_close(manager):
    await manager.start("local:embedding")
    await manager.shutdown()
    assert manager.active_count == 0
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_instances.py -q`
Expected: FAIL on missing manager.

- [ ] **Step 3: Implement per-model locks, profile selection, health refresh, route dependency tracking, and ordered shutdown**

- [ ] **Step 4: Verify instance tests pass**

Run: `.venv/bin/python -m pytest tests/test_local_ai_instances.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit instance lifecycle**

```bash
git add local_ai/runtimes/registry.py local_ai/instances tests/test_local_ai_instances.py
git commit -m "feat(local-ai): manage local model runtime instances"
```

### Task 12: VectorStore and Memory Integration

**Files:**
- Create: `local_ai/integration/embedding.py`
- Create: `local_ai/integration/reranker.py`
- Test: `tests/test_local_ai_memory_integration.py`
- Modify: `memory/vector_store.py`
- Modify: `memory/memory_manager.py`
- Modify: `core/bootstrap.py`

**Interfaces:**
- Produces: `LocalEmbeddingService.embed(texts) -> list[list[float]]`.
- Produces: `LocalRerankerService.score(query, documents) -> list[float]`.
- VectorStore consumes the service rather than probing model directories.

- [ ] **Step 1: Write failing compatibility and no-silent-fallback tests**

```python
@pytest.mark.asyncio
async def test_selected_local_embedding_instance_is_used(vector_store, embedding_service):
    await vector_store.embed(["hello"])
    assert embedding_service.calls == [["hello"]]

@pytest.mark.asyncio
async def test_stopped_local_reranker_reports_unavailable(memory_manager):
    with pytest.raises(LocalModelUnavailableError):
        await memory_manager.rerank_with_selected_local_model("q", ["a"])
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_memory_integration.py -q`
Expected: FAIL because integration services are missing.

- [ ] **Step 3: Inject local services while preserving remote and bundled-BGE compatibility**

Do not remove existing settings in this task. Translate them into built-in registry selections during bootstrap.

- [ ] **Step 4: Verify memory and vector regression suites**

Run: `.venv/bin/python -m pytest tests/test_local_ai_memory_integration.py tests/test_context_governance.py tests/test_local_embed_mode.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit memory integration**

```bash
git add local_ai/integration memory/vector_store.py memory/memory_manager.py core/bootstrap.py tests/test_local_ai_memory_integration.py
git commit -m "feat(local-ai): integrate local embedding and reranker services"
```

## Phase 4: Unified Provider Integration

### Task 13: Provider Catalog as Single Authority

**Files:**
- Create: `llm_gateway/__init__.py`
- Create: `llm_gateway/provider_catalog.py`
- Create: `llm_gateway/contracts.py`
- Test: `tests/test_provider_catalog.py`
- Modify: `config/provider_metadata.json`
- Modify: `config.py`

**Interfaces:**
- Produces: `ProviderCatalog.get(id)`, `list()`, `register(definition)`, `validate(definition)`, and metadata-based environment aliases.
- Produces: `ProviderDefinition`, `ProviderProtocol`, `ProviderCapabilities`, `AuthDefinition`, `EndpointDefinition`.

- [ ] **Step 1: Write failing metadata uniqueness, builtin protection, and alias tests**

```python
def test_provider_catalog_is_authoritative_for_known_provider_aliases(catalog):
    assert catalog.get("modelscope").auth.environment_aliases == ("MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_API_KEY")

def test_all_builtin_providers_are_protected(catalog):
    assert {p.id for p in catalog.list() if p.builtin} >= {"mimo", "agnes"}
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_provider_catalog.py -q`
Expected: FAIL because catalog is missing.

- [ ] **Step 3: Implement definitions loaded from one versioned metadata source**

Keep compatibility functions in `config.py` as catalog delegators. Include ModelScope token aliases to eliminate setup/startup drift.

- [ ] **Step 4: Verify catalog and config tests**

Run: `.venv/bin/python -m pytest tests/test_provider_catalog.py tests/test_model_switching_refactor.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit provider catalog**

```bash
git add llm_gateway config/provider_metadata.json config.py tests/test_provider_catalog.py
git commit -m "refactor(models): centralize provider definitions"
```

### Task 14: Complete Protocol Transports

**Files:**
- Create: `llm_gateway/transports/base.py`
- Create: `llm_gateway/transports/openai_compatible.py`
- Create: `llm_gateway/transports/anthropic.py`
- Create: `llm_gateway/transports/ollama.py`
- Create: `llm_gateway/transports/custom_mapping.py`
- Create: `llm_gateway/transports/local_ort.py`
- Test: `tests/test_provider_transports.py`
- Modify: `web/custom_providers.py`

**Interfaces:**
- All transports produce `complete(request) -> Completion`, `stream(request) -> AsyncIterator[CompletionChunk]`, `discover_models()`, and `health_check() -> CapabilityReport`.

- [ ] **Step 1: Write a shared failing contract suite for all five transports**

```python
@pytest.mark.parametrize("transport_name", ["openai", "anthropic", "ollama", "custom", "local_ort"])
@pytest.mark.asyncio
async def test_transport_stream_contract(transport_name, transport_factory):
    transport = transport_factory(transport_name)
    chunks = [chunk async for chunk in transport.stream(sample_request())]
    assert "".join(c.text for c in chunks) == "hello"
    assert chunks[-1].finish_reason == "stop"
```

- [ ] **Step 2: Verify native Anthropic streaming and custom mapping tests fail**

Run: `.venv/bin/python -m pytest tests/test_provider_transports.py -q`
Expected: FAIL for missing transports and current Anthropic stream rejection.

- [ ] **Step 3: Implement normalized streaming, tools, discovery fallback, errors, and safe custom field mapping**

Custom mapping accepts declarative JSON paths and header templates only. It does not execute user code or templates with arbitrary evaluation.

- [ ] **Step 4: Verify transport and existing Anthropic tests**

Run: `.venv/bin/python -m pytest tests/test_provider_transports.py tests/test_custom_anthropic_provider.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit transports**

```bash
git add llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py
git commit -m "feat(models): complete local cloud and custom transports"
```

### Task 15: Atomic Provider Onboarding and Route Validation

**Files:**
- Create: `llm_gateway/provider_service.py`
- Create: `web/routers/providers.py`
- Test: `tests/test_provider_onboarding.py`
- Modify: `web/routers/models.py`
- Modify: `web/routers/setup.py`
- Modify: `web/model_route_validator.py`
- Modify: `web/server.py`
- Modify: `web/agent_registry.py`

**Interfaces:**
- Produces: `ProviderService.test(draft) -> CapabilityReport`.
- Produces: `ProviderService.create(draft, credentials)`, `update(...)`, `delete(...)` with rollback.
- API: provider draft test, CRUD, capability report, model discovery, and route validation.

- [ ] **Step 1: Write failing atomicity and validation tests**

```python
@pytest.mark.asyncio
async def test_failed_provider_update_preserves_runtime_and_disk(service, config_service):
    before = config_service.get("models.providers")
    with pytest.raises(ProviderConnectionError):
        await service.update("custom", unreachable_draft())
    assert config_service.get("models.providers") == before

@pytest.mark.asyncio
async def test_route_rejects_disabled_provider(route_api):
    response = await route_api.put("/models/routes/chat", json={"provider": "disabled", "model": "x"})
    assert response.status_code == 409
```

- [ ] **Step 2: Verify tests fail against current non-atomic flows**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py -q`
Expected: FAIL on update rollback and route validation.

- [ ] **Step 3: Implement staged client construction, capability testing, credential transaction, config transaction, and runtime swap**

Migrate setup, startup restoration, model discovery, and sub-agent resolution to `ProviderCatalog`/`ProviderService`. Remove duplicated maps only after call sites migrate.

- [ ] **Step 4: Verify provider, route, setup, and credential tests**

Run: `.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_key_reading.py tests/test_model_switching_refactor.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit provider lifecycle**

```bash
git add llm_gateway/provider_service.py web/routers/providers.py web/routers/models.py web/routers/setup.py web/model_route_validator.py web/server.py web/agent_registry.py tests/test_provider_onboarding.py
git commit -m "feat(models): add atomic provider onboarding"
```

### Task 16: ModelRouter Local Transport Migration

**Files:**
- Test: `tests/test_model_router_local_transport.py`
- Modify: `model_router.py`
- Modify: `core/bootstrap.py`
- Modify: `agent_dispatcher.py`

**Interfaces:**
- Consumes: transport contracts and `ProviderService`.
- Keeps: public `ModelRouter.route()`, `chat_stream()`, and `set_chat_model()` compatibility.

- [ ] **Step 1: Write failing local selection and no-cross-provider-fallback tests**

```python
@pytest.mark.asyncio
async def test_selected_local_model_streams_through_instance(router, local_transport):
    router.set_chat_model("local-ort", "local:qwen-3b")
    assert "".join([c async for c in router.chat_stream(sample_messages())]) == "local"

@pytest.mark.asyncio
async def test_stopped_local_model_does_not_fallback_to_cloud(router):
    with pytest.raises(LocalModelUnavailableError):
        await collect(router.chat_stream(sample_messages()))
    assert router.cloud_client.call_count == 0
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_model_router_local_transport.py -q`
Expected: FAIL because local transport is not selectable.

- [ ] **Step 3: Delegate protocol/client details to transports while keeping route policy in ModelRouter**

- [ ] **Step 4: Verify model routing regression suites**

Run: `.venv/bin/python -m pytest tests/test_model_router_local_transport.py tests/test_model_router_truncation.py tests/test_model_router_stream_retry.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit router migration**

```bash
git add model_router.py core/bootstrap.py agent_dispatcher.py tests/test_model_router_local_transport.py
git commit -m "feat(models): route local ORT chat models"
```

## Phase 5: APIs and Existing Web UI

### Task 17: Local AI REST and WebSocket API

**Files:**
- Create: `web/routers/local_ai.py`
- Test: `tests/test_local_ai_api.py`
- Modify: `web/routers/local_deploy.py`
- Modify: `web/ws_hub.py`
- Modify: `web/server.py`

**Interfaces:**
- API resources: devices, catalog, models, downloads, instances, storage.
- WebSocket events: `local_ai_device_updated`, `local_ai_download_updated`, `local_ai_instance_updated`.

- [ ] **Step 1: Write failing authenticated resource and task-id tests**

```python
@pytest.mark.asyncio
async def test_download_create_returns_task_and_requires_destination(client):
    response = await client.post("/api/v1/local-ai/downloads", json={"model_id": "catalog:qwen", "destination": "/models"})
    assert response.status_code == 202
    assert response.json()["task"]["id"]

@pytest.mark.asyncio
async def test_legacy_devices_endpoint_contains_no_fixed_vip_label(client):
    payload = (await client.get("/api/v1/local-deploy/devices")).json()
    assert "3 TOPS INT8" not in json.dumps(payload)
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_api.py -q`
Expected: FAIL because resources are not mounted.

- [ ] **Step 3: Implement thin authenticated routers and legacy facade translation**

- [ ] **Step 4: Verify API, auth, and WebSocket tests**

Run: `.venv/bin/python -m pytest tests/test_local_ai_api.py tests/test_web_auth_enforcement.py tests/test_p1_ws_unregister.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit APIs**

```bash
git add web/routers/local_ai.py web/routers/local_deploy.py web/ws_hub.py web/server.py tests/test_local_ai_api.py
git commit -m "feat(web): expose local AI platform APIs"
```

### Task 18: Local AI Pinia Store and API Client

**Files:**
- Create: `web/frontend/src/stores/localAi.ts`
- Create: `web/frontend/src/api/localAi.ts`
- Test: `tests/test_frontend_local_ai_contracts.py`
- Modify: `web/frontend/src/api/ws.ts`

**Interfaces:**
- Produces store actions for load/rescan/download/pause/resume/cancel/start/stop/remove and directory preferences.

- [ ] **Step 1: Write failing source-contract tests for five resource collections and WebSocket reconciliation**

```python
def test_local_ai_store_reconciles_download_events():
    source = read_project_file("web/frontend/src/stores/localAi.ts")
    assert "local_ai_download_updated" in source
    assert "upsertDownload" in source
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py -q`
Expected: FAIL because store files are absent.

- [ ] **Step 3: Implement typed client, normalized entities, generation-safe loads, and WebSocket updates**

- [ ] **Step 4: Verify source contracts and TypeScript**

Run: `.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit`
Expected: tests PASS and type check exits 0.

- [ ] **Step 5: Commit frontend state layer**

```bash
git add web/frontend/src/stores/localAi.ts web/frontend/src/api/localAi.ts web/frontend/src/api/ws.ts tests/test_frontend_local_ai_contracts.py
git commit -m "feat(web): add local AI frontend state"
```

### Task 19: Five-Tab Local Deployment UI

**Files:**
- Create: `web/frontend/src/components/local-ai/DeploymentsTab.vue`
- Create: `web/frontend/src/components/local-ai/ModelMarketTab.vue`
- Create: `web/frontend/src/components/local-ai/InstalledModelsTab.vue`
- Create: `web/frontend/src/components/local-ai/ComputeDevicesTab.vue`
- Create: `web/frontend/src/components/local-ai/DownloadTasksTab.vue`
- Create: `web/frontend/src/components/local-ai/ModelDetailDrawer.vue`
- Create: `web/frontend/src/components/local-ai/StoragePickerDialog.vue`
- Modify: `web/frontend/src/views/LocalDeployView.vue`
- Test: `tests/test_frontend_local_ai_contracts.py`

**Interfaces:**
- Consumes: `useLocalAiStore()` only; components do not call raw HTTP.

- [ ] **Step 1: Extend failing contracts for tabs, dynamic summary, no hard-coded hardware, and storage preference**

```python
def test_local_deploy_has_five_tabs_and_no_fixed_device_copy():
    source = read_project_file("web/frontend/src/views/LocalDeployView.vue")
    for name in ("部署", "模型市场", "已安装", "算力设备", "下载任务"):
        assert name in source
    assert "Vivante VIP9000" not in source
    assert "3 TOPS INT8" not in source
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py -q`
Expected: FAIL on missing tabs/components.

- [ ] **Step 3: Implement focused Naive UI components using existing classes and CSS variables**

The storage dialog always appears without a saved default, exposes browse and manual entry, and stores the default only when its checkbox is selected. Installation completion opens the start-confirmation dialog.

- [ ] **Step 4: Verify contracts, type checking, and production build**

Run: `.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit && npm run build`
Expected: tests PASS, type check exits 0, build exits 0.

- [ ] **Step 5: Commit Local Deployment UI**

```bash
git add web/frontend/src/components/local-ai web/frontend/src/views/LocalDeployView.vue tests/test_frontend_local_ai_contracts.py
git commit -m "feat(web): add local model market and runtime tabs"
```

### Task 20: Unified Provider Onboarding UI

**Files:**
- Create: `web/frontend/src/components/models/ProviderWizard.vue`
- Create: `web/frontend/src/components/models/CapabilityMatrix.vue`
- Create: `web/frontend/src/components/models/CustomMappingEditor.vue`
- Create: `web/frontend/src/stores/providers.ts`
- Modify: `web/frontend/src/views/ModelsView.vue`
- Modify: `web/frontend/src/api/index.ts`
- Test: `tests/test_frontend_provider_contracts.py`

**Interfaces:**
- Consumes provider draft/test/create APIs.
- Provides four-step preset/local/custom onboarding and atomic save confirmation.

- [ ] **Step 1: Write failing source contracts for four protocols and test-before-save flow**

```python
def test_provider_wizard_exposes_all_supported_protocols():
    source = read_project_file("web/frontend/src/components/models/ProviderWizard.vue")
    for protocol in ("openai", "anthropic", "ollama", "custom-map"):
        assert protocol in source
    assert "CapabilityMatrix" in source
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q`
Expected: FAIL because components are absent.

- [ ] **Step 3: Implement wizard, test matrix, safe mapping editor, and model manual-entry fallback**

Use existing cards, modal controls, validation messages, and responsive layout. Do not add a new sidebar route.

- [ ] **Step 4: Verify contracts, type checking, and build**

Run: `.venv/bin/python -m pytest tests/test_frontend_provider_contracts.py -q && cd web/frontend && npx vue-tsc --noEmit && npm run build`
Expected: all commands exit 0.

- [ ] **Step 5: Commit provider UI**

```bash
git add web/frontend/src/components/models web/frontend/src/stores/providers.ts web/frontend/src/views/ModelsView.vue web/frontend/src/api/index.ts tests/test_frontend_provider_contracts.py
git commit -m "feat(web): add unified model provider onboarding"
```

## Phase 6: Packaging, Documentation, and End-to-End Verification

### Task 21: Cross-Platform Runtime Packaging

**Files:**
- Modify: `pyproject.toml`
- Modify: `xiaoda-agent.spec`
- Modify: `Dockerfile`
- Modify: `.github/workflows/build-release.yml`
- Modify: `scripts/build-release.sh`
- Test: `tests/test_windows_package_reliability.py`

**Interfaces:**
- Packaging includes runtime libraries and platform adapters, but excludes downloaded market models and `.part` files.

- [ ] **Step 1: Write failing packaging contract tests**

```python
def test_release_never_bundles_market_model_storage():
    spec = read_project_file("xiaoda-agent.spec")
    assert "local_model_storage" not in spec
    assert "*.part" not in spec

def test_ci_has_windows_linux_x64_and_linux_arm64_runtime_smoke_jobs():
    workflow = read_project_file(".github/workflows/build-release.yml")
    for platform in ("windows", "linux-x64", "linux-arm64"):
        assert platform in workflow
```

- [ ] **Step 2: Verify tests fail for missing ARM64/runtime gates**

Run: `.venv/bin/python -m pytest tests/test_windows_package_reliability.py -q`
Expected: FAIL on new packaging contracts.

- [ ] **Step 3: Add platform-marked optional dependencies, hidden imports, and smoke verification**

Do not claim an unavailable platform wheel works. CI must fail with an explicit dependency report when ORT GenAI lacks a supported artifact.

- [ ] **Step 4: Verify packaging contracts, Shell syntax, and YAML parsing**

Run: `.venv/bin/python -m pytest tests/test_windows_package_reliability.py -q && bash -n scripts/build-release.sh && .venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/build-release.yml'))"`
Expected: all commands exit 0.

- [ ] **Step 5: Commit packaging changes**

```bash
git add pyproject.toml xiaoda-agent.spec Dockerfile .github/workflows/build-release.yml scripts/build-release.sh tests/test_windows_package_reliability.py
git commit -m "build: package cross-platform local AI runtimes"
```

### Task 22: Operator and User Documentation

**Files:**
- Create: `docs/local-ai-platform.md`
- Modify: `README.md`
- Modify: `.env.example`
- Test: `tests/test_local_ai_docs_contract.py`

**Interfaces:**
- Documents platform providers, ModelScope source policy, storage behavior, recovery, NPU evidence, and Android scope.

- [ ] **Step 1: Write failing documentation contracts**

```python
def test_docs_state_android_is_contract_only():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "Android" in docs
    assert "不包含 Android 客户端" in docs

def test_docs_never_claim_fixed_vip_tops():
    docs = read_project_file("docs/local-ai-platform.md")
    assert "VIP9000 (3 TOPS" not in docs
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/python -m pytest tests/test_local_ai_docs_contract.py -q`
Expected: FAIL because documentation is absent.

- [ ] **Step 3: Write exact setup, provider, model storage, recovery, and troubleshooting documentation**

- [ ] **Step 4: Verify documentation contracts**

Run: `.venv/bin/python -m pytest tests/test_local_ai_docs_contract.py -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit documentation**

```bash
git add docs/local-ai-platform.md README.md .env.example tests/test_local_ai_docs_contract.py
git commit -m "docs: document local AI platform"
```

### Task 23: Full Verification and Compatibility Cleanup

**Files:**
- Modify only files proven unused by reference searches after all migrations.
- Test: all Python and frontend suites.

**Interfaces:**
- Completion evidence for every global constraint and design acceptance item.

- [ ] **Step 1: Search for duplicate authorities and hard-coded claims**

Run:

```bash
rg "Vivante VIP9000|3 TOPS INT8|providers=\[\"CPUExecutionProvider\"\]|_KNOWN_PROVIDERS" --glob '!docs/superpowers/**' --glob '!tests/**'
```

Expected: no runtime/UI hard-coded device claim; known-provider compatibility constants either delegate to `ProviderCatalog` or have no callers.

- [ ] **Step 2: Run focused local AI and provider suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_local_ai_contracts.py \
  tests/test_local_ai_system_probe.py \
  tests/test_local_ai_device_registry.py \
  tests/test_local_ai_catalog.py \
  tests/test_local_ai_modelscope.py \
  tests/test_local_ai_storage.py \
  tests/test_local_ai_model_registry.py \
  tests/test_local_ai_downloads.py \
  tests/test_local_ai_ort_runtimes.py \
  tests/test_local_ai_ort_genai.py \
  tests/test_local_ai_instances.py \
  tests/test_local_ai_memory_integration.py \
  tests/test_provider_catalog.py \
  tests/test_provider_transports.py \
  tests/test_provider_onboarding.py \
  tests/test_model_router_local_transport.py \
  tests/test_local_ai_api.py \
  tests/test_frontend_local_ai_contracts.py \
  tests/test_frontend_provider_contracts.py -q
```

Expected: all tests PASS.

- [ ] **Step 3: Run the complete Python regression suite**

Run: `.venv/bin/python -m pytest tests/ --timeout=60 -k "not e2e_real_scenario" -q`
Expected: zero failures.

- [ ] **Step 4: Verify frontend, scripts, workflow, and source diagnostics**

Run:

```bash
cd web/frontend && npx vue-tsc --noEmit && npm run build
cd ../.. && bash -n scripts/*.sh
.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/build-release.yml'))"
git diff --check -- . ':!cli.py'
```

Expected: every command exits 0; IDE diagnostics contain no errors.

- [ ] **Step 5: Perform browser acceptance on the actual Web UI**

Verify authenticated flows: real device scan, no invented NPU label, catalog filtering, custom repository inspection, first destination prompt, unsaved destination prompting again, saved default reuse, download progress, pause/resume, hash failure quarantine, install, start confirmation, local chat stream, embedding selection, reranker selection, provider onboarding for four protocols, stop, and removal protection.

- [ ] **Step 6: Review uncommitted changes and commit cleanup**

```bash
git status --short
git diff --stat
git add <only files belonging to this implementation>
git commit -m "refactor: complete local AI platform migration"
```

Do not include the user's unrelated `cli.py` change.
