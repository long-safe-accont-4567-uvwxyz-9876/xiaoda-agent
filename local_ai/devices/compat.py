"""模型可跑性评估：根据已探测算力设备自动标识本地模型能否运行。

评估依据：
- 模型 compatibility.runtimes（ort / ort_genai / vip）与 providers；
- 已探测设备上的 healthy backend（CPU / CUDA / ROCm / DirectML / VIPLite）；
- 运行时资源需求（minimum_ram / minimum_vram）与设备可用内存对比。

评估是展示性标注，不阻止下载/安装；内存不足时给出 reason 供前端提示。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from local_ai.contracts import CatalogModel, ComputeDevice


def _string_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _allowed(compatibility: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    for key in keys:
        values = _string_values(compatibility.get(key))
        if values:
            return values
    return ()


_GPU_PROVIDERS = frozenset({
    "CUDAExecutionProvider",
    "TensorrtExecutionProvider",
    "ROCMExecutionProvider",
    "DmlExecutionProvider",
})

# runtime 是否可承载于某种设备
_RUNTIME_ON_CPU = frozenset({"ort", "ort_genai"})
_RUNTIME_ON_GPU = frozenset({"ort", "ort_genai"})
_RUNTIME_ON_NPU = frozenset({"vip"})


def _device_snapshot(devices: list[ComputeDevice]) -> dict[str, Any]:
    cpu = next((device for device in devices if device.kind == "cpu"), None)
    gpu_backends: list[str] = []
    npu_backends: list[str] = []
    for device in devices:
        for backend in device.backends:
            if not backend.healthy:
                continue
            if device.kind == "gpu" and backend.provider in _GPU_PROVIDERS:
                gpu_backends.append(backend.provider)
            elif device.kind == "npu":
                npu_backends.append(backend.provider)
    return {
        "cpu": cpu,
        "gpu_backends": tuple(gpu_backends),
        "npu_backends": tuple(npu_backends),
    }


def evaluate_runnability(
    model: CatalogModel,
    devices: list[ComputeDevice],
) -> dict[str, Any]:
    """评估单个目录模型在已探测设备上的可跑性。

    返回 {cpu, gpu, npu, gpu_provider, reason}：
    - cpu/gpu/npu：布尔，对应设备类型是否可运行；
    - gpu_provider：首个可用 GPU 提供程序（如 CUDAExecutionProvider）；
    - reason：当全部不可用时的人类可读原因（空字符串即可运行）。
    """
    runtimes = _allowed(getattr(model, "compatibility", {}) or {}, "runtimes", "runtime")
    snapshot = _device_snapshot(devices)
    requirements = getattr(model, "runtime_requirements", {}) or {}
    min_ram = requirements.get("minimum_ram", 0)
    min_vram = requirements.get("minimum_vram", 0)

    result: dict[str, Any] = {"cpu": False, "gpu": False, "npu": False, "gpu_provider": None}
    reasons: list[str] = []

    cpu = snapshot["cpu"]
    if _RUNTIME_ON_CPU & set(runtimes):
        ram_ok = True
        if cpu is not None and cpu.memory_total and min_ram > cpu.memory_total:
            ram_ok = False
            reasons.append(f"内存不足（需 ≥ {min_ram / 1024**3:.1f} GiB）")
        result["cpu"] = ram_ok

    if _RUNTIME_ON_GPU & set(runtimes):
        if snapshot["gpu_backends"]:
            vram_ok = True
            gpu = next(
                (device for device in devices if device.kind == "gpu"),
                None,
            )
            if gpu is not None and gpu.memory_total and min_vram > gpu.memory_total:
                vram_ok = False
                reasons.append(f"显存不足（需 ≥ {min_vram / 1024**3:.1f} GiB）")
            result["gpu"] = vram_ok
            result["gpu_provider"] = snapshot["gpu_backends"][0]
        else:
            reasons.append("未探测到可用 GPU")
    elif runtimes and not (_RUNTIME_ON_GPU & set(runtimes)):
        pass

    if _RUNTIME_ON_NPU & set(runtimes):
        result["npu"] = bool(snapshot["npu_backends"])
        if not snapshot["npu_backends"]:
            reasons.append("未探测到可用 NPU")

    if not runtimes:
        # 无兼容性声明：保守认为至少 CPU 可跑
        result["cpu"] = True

    # 用途级覆盖：healthy backend 显式声明了可承载的模型用途时
    # （如 VIP NPU 仅支持 embedding），即使模型的 runtimes 声明未包含该
    # backend 的 runtime，也认为对应设备可跑——系统实际正是通过 vip runner
    # 在 NPU 上运行这些 embedding 模型。
    for device in devices:
        if not device.backends:
            continue
        for backend in device.backends:
            if not backend.healthy or not backend.purposes:
                continue
            if model.purpose not in backend.purposes:
                continue
            if device.kind == "npu":
                result["npu"] = True
            elif device.kind == "gpu":
                result["gpu"] = True
                result["gpu_provider"] = result["gpu_provider"] or backend.provider

    result["reason"] = "；".join(reasons)
    return result


def annotate_catalog_models(
    models: list[CatalogModel],
    devices: list[ComputeDevice],
) -> list[dict[str, Any]]:
    """批量附加可跑性标注（供 /local-ai/catalog 返回）。"""
    annotated = []
    for model in models:
        record = model.to_dict()
        # 兼容性声明缺失时（如测试用的轻量 Record）不附加可跑性标注
        if hasattr(model, "compatibility"):
            record["runnable"] = evaluate_runnability(model, devices)
        annotated.append(record)
    return annotated


__all__ = ["evaluate_runnability", "annotate_catalog_models"]
