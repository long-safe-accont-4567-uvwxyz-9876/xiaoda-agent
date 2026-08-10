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

