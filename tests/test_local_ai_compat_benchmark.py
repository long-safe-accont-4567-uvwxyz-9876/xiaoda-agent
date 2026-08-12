"""可跑性评估（devices/compat.py）与实例测速（instances/manager.benchmark）测试。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from local_ai.contracts import (
    CatalogModel,
    ComputeDevice,
    DeviceState,
    ExecutionBackend,
    ModelPurpose,
    RuntimeKind,
)
from local_ai.devices.compat import annotate_catalog_models, evaluate_runnability
from local_ai.instances.manager import InstanceManager


def _catalog_model(**overrides: Any) -> CatalogModel:
    values: dict[str, Any] = {
        "id": "test-model",
        "source": "curated",
        "repository": "org/test",
        "revision": "a" * 40,
        "purpose": ModelPurpose.EMBEDDING,
        "files": [{"path": "model.onnx", "size": 100, "sha256": "b" * 64}],
        "download_size": 100,
        "compatibility": {"runtimes": ["ort"], "providers": ["CPUExecutionProvider"]},
        "runtime_requirements": {"minimum_ram": 0, "minimum_vram": 0},
    }
    values.update(overrides)
    return CatalogModel(**values)


def _device(**overrides: Any) -> ComputeDevice:
    values: dict[str, Any] = {
        "id": "cpu:0",
        "name": "CPU",
        "kind": "cpu",
        "architecture": "x86_64",
        "state": DeviceState.AVAILABLE,
        "memory_total": 16 * 1024**3,
        "memory_available": 8 * 1024**3,
        "backends": [
            ExecutionBackend(
                runtime=RuntimeKind.ORT,
                provider="CPUExecutionProvider",
                healthy=True,
            )
        ],
        "system": {"platform": "linux"},
        "evidence": {},
    }
    values.update(overrides)
    return ComputeDevice(**values)


def _gpu_device() -> ComputeDevice:
    return _device(
        id="nvidia:gpu0",
        name="RTX 4060",
        kind="gpu",
        memory_total=8 * 1024**3,
        memory_available=6 * 1024**3,
        backends=[
            ExecutionBackend(
                runtime=RuntimeKind.ORT,
                provider="CUDAExecutionProvider",
                healthy=True,
                options={"device_id": 0},
            )
        ],
        evidence={"vendor": "nvidia"},
    )


def _npu_device() -> ComputeDevice:
    return _device(
        id="npu:vip:0",
        name="VIP NPU",
        kind="npu",
        backends=[
            ExecutionBackend(
                runtime=RuntimeKind.VIP,
                provider="VIPLite",
                healthy=True,
                purposes=(ModelPurpose.EMBEDDING,),
            )
        ],
        evidence={"available": True},
    )


class TestEvaluateRunnability:
    def test_cpu_only_model_runs_on_cpu(self) -> None:
        result = evaluate_runnability(
            _catalog_model(), [_device(), _gpu_device(), _npu_device()]
        )
        assert result["cpu"] is True
        assert result["npu"] is True  # VIP NPU 声明 embedding 用途 → 用途级覆盖
        assert result["gpu"] is True  # CUDA backend + runtimes 含 ort
        assert result["reason"] == ""

    def test_vip_model_requires_npu(self) -> None:
        model = _catalog_model(
            compatibility={"runtimes": ["vip"], "providers": ["VIPLite"]}
        )
        result = evaluate_runnability(model, [_device()])
        assert result["npu"] is False
        assert "NPU" in result["reason"]
        result = evaluate_runnability(model, [_device(), _npu_device()])
        assert result["npu"] is True

    def test_embedding_model_runs_on_vip_npu_without_declaration(self) -> None:
        # 无 compatibility 声明时保守 CPU 可跑；VIP NPU 声明 embedding 用途 → npu 也可跑
        model = _catalog_model(compatibility={})
        result = evaluate_runnability(model, [_device(), _npu_device()])
        assert result["cpu"] is True
        assert result["npu"] is True
        assert result["gpu"] is False

    def test_ort_genai_chat_runs_on_cpu(self) -> None:
        model = _catalog_model(
            purpose=ModelPurpose.CHAT,
            compatibility={"runtimes": ["ort_genai"], "providers": ["cpu"]},
        )
        result = evaluate_runnability(model, [_device()])
        assert result["cpu"] is True

    def test_ram_shortage_blocks_cpu(self) -> None:
        model = _catalog_model(
            compatibility={"runtimes": ["ort"]},
            runtime_requirements={"minimum_ram": 64 * 1024**3},
        )
        result = evaluate_runnability(model, [_device()])
        assert result["cpu"] is False
        assert "内存不足" in result["reason"]

    def test_no_compatibility_declared_defaults_to_cpu(self) -> None:
        model = _catalog_model(compatibility={})
        result = evaluate_runnability(model, [_device()])
        assert result["cpu"] is True

    def test_annotate_skips_records_without_compatibility(self) -> None:
        class PlainRecord:
            def to_dict(self) -> dict[str, Any]:
                return {"id": "x"}

        annotated = annotate_catalog_models([PlainRecord()], [_device()])
        assert annotated[0] == {"id": "x"}


class _FakeRuntime:
    """记录调用的假运行时（embed / score / stream）。"""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        self.calls += 1
        return [[0.1, 0.2]] * len(texts)

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.calls += 1
        return [0.5] * len(documents)

    def health(self) -> bool:
        return True


class _FakeManager(InstanceManager):
    """隔离 benchmark 的假管理器：预置已运行的 embedding 实例。"""

    def __init__(self) -> None:
        # 参数仅保存引用不调用方法，可安全传入 None
        super().__init__(None, None, None)
        self.runtime = _FakeRuntime()
        self.instance = type(
            "Instance",
            (),
            {"id": "instance:fake", "device_id": "cpu:0"},
        )()
        self._model_locks: dict[str, asyncio.Lock] = {}
        self._model_instances = {"test-model": "instance:fake"}
        self._instances = {"instance:fake": self.instance}
        self._runtimes = {"instance:fake": self.runtime}
        self._instance_purposes = {"instance:fake": ModelPurpose.EMBEDDING}
        self._selected_instances = {ModelPurpose.EMBEDDING: "instance:fake"}
        self._selection_generations = {ModelPurpose.EMBEDDING: 1}

    async def _run_sync(self, model_id: str, function: Any, *args: Any) -> Any:
        return await asyncio.to_thread(function, *args)


@pytest.mark.asyncio
async def test_benchmark_embedding_measures_throughput() -> None:
    manager = _FakeManager()
    result = await manager.benchmark("test-model")
    assert result["ok"] is True
    assert result["purpose"] == "embedding"
    assert result["latency_ms"] > 0
    assert result["samples_per_second"] > 0
    assert result["dimensions"] == 2
    assert manager.runtime.calls == 3


@pytest.mark.asyncio
async def test_benchmark_rejects_invalid_iterations() -> None:
    manager = _FakeManager()
    with pytest.raises(ValueError):
        await manager.benchmark("test-model", iterations=0)
