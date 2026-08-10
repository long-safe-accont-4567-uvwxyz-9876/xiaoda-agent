import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from local_ai.contracts import (
    CatalogModel,
    ComputeDevice,
    DeviceState,
    DownloadTask,
    ExecutionBackend,
    InstalledModel,
    ModelInstance,
    ModelPurpose,
    RuntimeKind,
    RuntimeProfile,
    TaskState,
)

NOW = datetime(2026, 8, 9, 8, 30, tzinfo=timezone.utc)


def test_compute_device_round_trip_preserves_backend_evidence():
    device = ComputeDevice(
        id="cpu:0",
        name="ARM Cortex-A76",
        kind="cpu",
        architecture="aarch64",
        state=DeviceState.AVAILABLE,
        memory_total=8_000_000_000,
        memory_available=6_000_000_000,
        backends=(
            ExecutionBackend(
                runtime=RuntimeKind.ORT,
                provider="CPUExecutionProvider",
                healthy=True,
                evidence={"probe": "session"},
            ),
        ),
        evidence={"source": "/proc/cpuinfo"},
    )

    payload = device.to_dict()

    assert payload["state"] == "available"
    assert payload["backends"][0]["runtime"] == "ort"
    assert ComputeDevice.from_dict(payload) == device


@pytest.mark.parametrize(
    "revision",
    ("", "main", "abc123", "g123456", "a" * 65, "1234567 "),
)
def test_catalog_model_requires_7_to_64_character_hex_revision(revision):
    with pytest.raises(ValueError):
        CatalogModel(
            id="bad",
            source="modelscope",
            repository="owner/model",
            revision=revision,
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
        )


def test_catalog_model_requires_files_with_checksums():
    with pytest.raises(ValueError):
        CatalogModel(
            id="bad",
            source="modelscope",
            repository="owner/model",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": 1},),
        )


@pytest.mark.parametrize("revision", ["abcdef0", "A" * 64])
def test_catalog_model_accepts_revision_boundaries(revision):
    model = CatalogModel(
        id="model:one",
        source="modelscope",
        repository="owner/model",
        revision=revision,
        purpose=ModelPurpose.CHAT,
        files=({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
    )

    assert model.revision == revision


def test_installed_model_requires_7_to_64_character_hex_revision():
    with pytest.raises(ValueError, match="revision"):
        InstalledModel(
            id="local:one",
            catalog_id="model:one",
            revision="main",
            purpose=ModelPurpose.EMBEDDING,
            directory="/models/one",
            manifest_checksum="def",
            validation_state="valid",
            ownership="user",
            installed_at=NOW,
        )


def test_compute_device_normalizes_backend_mappings():
    device = ComputeDevice(
        id="cpu:0",
        name="CPU",
        kind="cpu",
        architecture="x86_64",
        state=DeviceState.AVAILABLE,
        memory_total=1024,
        memory_available=512,
        backends=({"runtime": "ort", "provider": "CPUExecutionProvider", "healthy": True},),
    )

    assert device.backends == (
        ExecutionBackend(runtime=RuntimeKind.ORT, provider="CPUExecutionProvider", healthy=True),
    )


def test_compute_device_rejects_invalid_backend_values():
    with pytest.raises(ValueError, match="backends"):
        ComputeDevice(
            id="cpu:0",
            name="CPU",
            kind="cpu",
            architecture="x86_64",
            state=DeviceState.AVAILABLE,
            memory_total=1024,
            memory_available=512,
            backends=("CPUExecutionProvider",),
        )


def test_all_records_round_trip_with_json_safe_values():
    records = (
        ExecutionBackend(
            runtime=RuntimeKind.ORT_GENAI,
            provider="CUDAExecutionProvider",
            healthy=True,
            options={"device_id": 0},
            purposes=(ModelPurpose.CHAT,),
            precisions=("int4",),
        ),
        CatalogModel(
            id="model:qwen",
            source="modelscope",
            repository="owner/qwen",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": 128, "sha256": "a" * 64},),
            download_size=128,
        ),
        InstalledModel(
            id="local:qwen",
            catalog_id="model:qwen",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            directory="/models/qwen",
            manifest_checksum="def",
            validation_state="valid",
            ownership="user",
            installed_at=NOW,
        ),
        DownloadTask(
            id="task:1",
            model_id="model:qwen",
            state=TaskState.DOWNLOADING,
            bytes_downloaded=64,
            total_bytes=128,
            speed_bps=32.5,
            eta_seconds=2.0,
            resumable=True,
            destination="/models/qwen",
            created_at=NOW,
            updated_at=NOW,
        ),
        RuntimeProfile(
            runtime=RuntimeKind.ORT_GENAI,
            device_id="gpu:0",
            provider="CUDAExecutionProvider",
            options={"threads": 2},
            estimated_ram=1024,
            estimated_vram=512,
            allow_fallback=True,
        ),
        ModelInstance(
            id="instance:1",
            model_id="local:qwen",
            runtime=RuntimeKind.ORT_GENAI,
            device_id="gpu:0",
            state="running",
            health="healthy",
            active_routes=("chat",),
            resource_usage={"memory": 512},
            started_at=NOW,
            updated_at=NOW,
        ),
    )

    for record in records:
        payload = record.to_dict()
        json.dumps(payload, allow_nan=False)
        assert type(record).from_dict(payload) == record


def test_runtime_profile_rejects_legacy_estimated_memory_payload():
    with pytest.raises(TypeError, match="estimated_memory"):
        RuntimeProfile.from_dict(
            {
                "runtime": "ort",
                "device_id": "cpu:0",
                "provider": "CPUExecutionProvider",
                "estimated_memory": 1,
            }
        )


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: ExecutionBackend(runtime=RuntimeKind.ORT, provider="", healthy=True), "provider"),
        (
            lambda: ComputeDevice(
                id="cpu:0",
                name="CPU",
                kind="cpu",
                architecture="x86_64",
                state=DeviceState.AVAILABLE,
                memory_total=-1,
                memory_available=0,
            ),
            "memory_total",
        ),
        (
            lambda: DownloadTask(
                id="task:1",
                model_id="model:1",
                state=TaskState.PENDING,
                bytes_downloaded=2,
                total_bytes=1,
                destination="/models/one",
                created_at=NOW,
                updated_at=NOW,
            ),
            "bytes_downloaded",
        ),
        (
            lambda: InstalledModel(
                id="local:one",
                catalog_id="model:one",
                revision="abc1234",
                purpose=ModelPurpose.EMBEDDING,
                directory="models/one",
                manifest_checksum="def",
                validation_state="valid",
                ownership="user",
                installed_at=NOW,
            ),
            "directory",
        ),
        (
            lambda: ModelInstance(
                id="instance:1",
                model_id="local:one",
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                state="running",
                health="healthy",
                started_at=datetime(2026, 8, 9, 8, 30),
                updated_at=NOW,
            ),
            "started_at",
        ),
    ],
)
def test_records_reject_invalid_transport_values(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


def test_records_are_immutable_and_copy_mutable_inputs():
    evidence = {"probe": {"ok": True}}
    backend = ExecutionBackend(
        runtime=RuntimeKind.ORT,
        provider="CPUExecutionProvider",
        healthy=True,
        evidence=evidence,
    )
    evidence["probe"]["ok"] = False

    assert backend.to_dict()["evidence"] == {"probe": {"ok": True}}
    with pytest.raises((AttributeError, TypeError)):
        backend.provider = "other"


@pytest.mark.parametrize("unsafe", [float("nan"), float("inf"), Path("model.onnx")])
def test_nested_transport_values_reject_non_json_values(unsafe):
    with pytest.raises(ValueError, match="JSON"):
        ExecutionBackend(
            runtime=RuntimeKind.ORT,
            provider="CPUExecutionProvider",
            healthy=True,
            evidence={"unsafe": unsafe},
        )


def test_to_dict_is_strict_json_serializable():
    backend = ExecutionBackend(
        runtime=RuntimeKind.ORT,
        provider="CPUExecutionProvider",
        healthy=True,
        evidence={"nested": [None, True, 1, 1.5, "ok"]},
    )

    assert json.loads(json.dumps(backend.to_dict(), allow_nan=False)) == backend.to_dict()


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: InstalledModel(
                id="local:one",
                catalog_id="model:one",
                revision="abc1234",
                purpose=ModelPurpose.CHAT,
                directory="/models/one",
                manifest_checksum="def",
                validation_state="valid",
                ownership="user",
                installed_at=None,
            ),
            "installed_at",
        ),
        (
            lambda: DownloadTask(
                id="task:1",
                model_id="model:one",
                state=TaskState.PENDING,
                bytes_downloaded=0,
                total_bytes=1,
                destination="/models/one",
                created_at=None,
                updated_at=NOW,
            ),
            "created_at",
        ),
        (
            lambda: DownloadTask(
                id="task:1",
                model_id="model:one",
                state=TaskState.PENDING,
                bytes_downloaded=0,
                total_bytes=1,
                destination="/models/one",
                created_at=NOW,
                updated_at=None,
            ),
            "updated_at",
        ),
        (
            lambda: ModelInstance(
                id="instance:1",
                model_id="local:one",
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                state="running",
                health="healthy",
                started_at=None,
                updated_at=NOW,
            ),
            "started_at",
        ),
        (
            lambda: ModelInstance(
                id="instance:1",
                model_id="local:one",
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                state="running",
                health="healthy",
                started_at=NOW,
                updated_at=None,
            ),
            "updated_at",
        ),
    ],
)
def test_required_utc_timestamps_reject_none(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize("invalid", [True, False, 1.0])
@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda value: ComputeDevice(
                id="cpu:0",
                name="CPU",
                kind="cpu",
                architecture="x86_64",
                state=DeviceState.AVAILABLE,
                memory_total=value,
                memory_available=0,
            ),
            "memory_total",
        ),
        (
            lambda value: CatalogModel(
                id="model:one",
                source="modelscope",
                repository="owner/model",
                revision="abc1234",
                purpose=ModelPurpose.CHAT,
                files=({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
                parameter_count=value,
            ),
            "parameter_count",
        ),
        (
            lambda value: DownloadTask(
                id="task:1",
                model_id="model:one",
                state=TaskState.PENDING,
                bytes_downloaded=0,
                total_bytes=value,
                destination="/models/one",
                created_at=NOW,
                updated_at=NOW,
            ),
            "total_bytes",
        ),
        (
            lambda value: RuntimeProfile(
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                provider="CPUExecutionProvider",
                estimated_ram=value,
            ),
            "estimated_ram",
        ),
    ],
)
def test_integer_fields_reject_bool_and_float(factory, match, invalid):
    with pytest.raises(ValueError, match=match):
        factory(invalid)


@pytest.mark.parametrize("invalid", [True, False, 1.0])
def test_manifest_size_rejects_bool_and_float(invalid):
    with pytest.raises(ValueError, match="files.size"):
        CatalogModel(
            id="model:one",
            source="modelscope",
            repository="owner/model",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": invalid, "sha256": "a" * 64},),
        )


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: ExecutionBackend(
                runtime=RuntimeKind.ORT,
                provider="CPUExecutionProvider",
                healthy=1,
            ),
            "healthy",
        ),
        (
            lambda: DownloadTask(
                id="task:1",
                model_id="model:one",
                state=TaskState.PENDING,
                bytes_downloaded=0,
                total_bytes=1,
                destination="/models/one",
                created_at=NOW,
                updated_at=NOW,
                resumable=1,
            ),
            "resumable",
        ),
        (
            lambda: RuntimeProfile(
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                provider="CPUExecutionProvider",
                allow_fallback=1,
            ),
            "allow_fallback",
        ),
    ],
)
def test_boolean_fields_require_exact_bool(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: ExecutionBackend(
                runtime=RuntimeKind.ORT,
                provider="CPUExecutionProvider",
                healthy=True,
                precisions="int4",
            ),
            "precisions",
        ),
        (
            lambda: ModelInstance(
                id="instance:1",
                model_id="local:one",
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                state="running",
                health="healthy",
                started_at=NOW,
                updated_at=NOW,
                active_routes="chat",
            ),
            "active_routes",
        ),
    ],
)
def test_string_sequences_reject_bare_strings(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize("invalid", ["", "   ", 1])
@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda value: ExecutionBackend(
                runtime=RuntimeKind.ORT,
                provider="CPUExecutionProvider",
                healthy=True,
                precisions=(value,),
            ),
            "precisions",
        ),
        (
            lambda value: ModelInstance(
                id="instance:1",
                model_id="local:one",
                runtime=RuntimeKind.ORT,
                device_id="cpu:0",
                state="running",
                health="healthy",
                started_at=NOW,
                updated_at=NOW,
                active_routes=(value,),
            ),
            "active_routes",
        ),
    ],
)
def test_string_sequences_require_non_empty_string_items(factory, match, invalid):
    with pytest.raises(ValueError, match=match):
        factory(invalid)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: ExecutionBackend.from_dict(
                {
                    "runtime": "ort",
                    "provider": "CPUExecutionProvider",
                    "healthy": True,
                    "precisions": "int4",
                }
            ),
            "precisions",
        ),
        (
            lambda: ModelInstance.from_dict(
                {
                    "id": "instance:1",
                    "model_id": "local:one",
                    "runtime": "ort",
                    "device_id": "cpu:0",
                    "state": "running",
                    "health": "healthy",
                    "started_at": "2026-08-09T08:30:00Z",
                    "updated_at": "2026-08-09T08:30:00Z",
                    "active_routes": "chat",
                }
            ),
            "active_routes",
        ),
    ],
)
def test_from_dict_string_sequences_reject_bare_strings(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize("directory", ["/models/one", r"C:\models\one", r"\\server\share\one"])
def test_installed_model_accepts_cross_host_absolute_paths(directory):
    model = InstalledModel(
        id="local:one",
        catalog_id="model:one",
        revision="abc1234",
        purpose=ModelPurpose.CHAT,
        directory=directory,
        manifest_checksum="def",
        validation_state="valid",
        ownership="user",
        installed_at=NOW,
    )

    assert model.directory == directory


@pytest.mark.parametrize("directory", ["models/one", r"C:models\one", r"\server\share\one"])
def test_installed_model_rejects_cross_host_relative_paths(directory):
    with pytest.raises(ValueError, match="directory"):
        InstalledModel(
            id="local:one",
            catalog_id="model:one",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            directory=directory,
            manifest_checksum="def",
            validation_state="valid",
            ownership="user",
            installed_at=NOW,
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CatalogModel(
            id="model:one",
            source="modelscope",
            repository="owner/model",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
            quantization=1,
        ),
        lambda: CatalogModel(
            id="model:one",
            source="modelscope",
            repository="owner/model",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
            license=1,
        ),
        lambda: DownloadTask(
            id="task:1",
            model_id="model:one",
            state=TaskState.FAILED,
            bytes_downloaded=0,
            total_bytes=1,
            destination="/models/one",
            created_at=NOW,
            updated_at=NOW,
            error=1,
        ),
    ],
)
def test_optional_string_fields_reject_non_strings(factory):
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize("sha256", ["abc", "g" * 64, "a" * 63, "a" * 65])
def test_catalog_manifest_requires_64_character_hex_sha256(sha256):
    with pytest.raises(ValueError, match="sha256"):
        CatalogModel(
            id="model:one",
            source="modelscope",
            repository="owner/model",
            revision="abc1234",
            purpose=ModelPurpose.CHAT,
            files=({"path": "model.onnx", "size": 1, "sha256": sha256},),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ExecutionBackend(RuntimeKind.ORT, "CPUExecutionProvider", True, options=[]),
        lambda: ExecutionBackend(RuntimeKind.ORT, "CPUExecutionProvider", True, evidence=[]),
        lambda: ComputeDevice("cpu:0", "CPU", "cpu", "x86_64", DeviceState.AVAILABLE, 1, 1, system=[]),
        lambda: ComputeDevice("cpu:0", "CPU", "cpu", "x86_64", DeviceState.AVAILABLE, 1, 1, evidence=[]),
        lambda: CatalogModel(
            "model:one",
            "modelscope",
            "owner/model",
            "abc1234",
            ModelPurpose.CHAT,
            ({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
            compatibility=[],
        ),
        lambda: CatalogModel(
            "model:one",
            "modelscope",
            "owner/model",
            "abc1234",
            ModelPurpose.CHAT,
            ({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
            runtime_requirements=[],
        ),
        lambda: InstalledModel(
            "local:one",
            "model:one",
            "abc1234",
            ModelPurpose.CHAT,
            "/models/one",
            "def",
            "valid",
            "user",
            NOW,
            metadata=[],
        ),
        lambda: RuntimeProfile(RuntimeKind.ORT, "cpu:0", "CPUExecutionProvider", options=[]),
        lambda: ModelInstance(
            "instance:1",
            "local:one",
            RuntimeKind.ORT,
            "cpu:0",
            "running",
            "healthy",
            NOW,
            NOW,
            resource_usage=[],
        ),
    ],
)
def test_mapping_fields_reject_non_mappings(factory):
    with pytest.raises(ValueError, match="mapping"):
        factory()
