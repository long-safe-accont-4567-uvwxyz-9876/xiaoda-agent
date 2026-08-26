from __future__ import annotations

import os
import platform
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from local_ai.contracts import (
    CatalogModel,
    ComputeDevice,
    DeviceState,
    ExecutionBackend,
    RuntimeProfile,
)
from local_ai.devices.ort_providers import OrtProviderProbe
from local_ai.devices.system_probe import probe_system_devices
from local_ai.runtimes.session_tuning import provider_rank


class IncompatibleBackendError(RuntimeError):
    pass


class InvalidResourceRequirementsError(ValueError):
    pass


def _provider_device_id(provider: str) -> str:
    if provider == "CPUExecutionProvider":
        return "cpu:0"
    suffix = provider.removesuffix("ExecutionProvider").casefold()
    return f"ort:{suffix}"


# 云端/远程推理提供器不属于本地算力设备，探测时必须排除，
# 否则会以 degraded 状态混入设备列表误导用户
_NON_LOCAL_PROVIDERS = frozenset({"AzureExecutionProvider"})


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


def _resource_requirements(model: CatalogModel) -> tuple[int, int, int, int]:
    requirements = model.runtime_requirements
    for key in ("minimum_memory", "min_memory", "recommended_memory"):
        value = requirements.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise InvalidResourceRequirementsError(
                f"{key} must be a non-negative integer"
            )
        if type(value) is int and value > 0:
            raise InvalidResourceRequirementsError(
                f"{key} is ambiguous; use typed RAM/VRAM requirements"
            )
    values: dict[str, int] = {}
    for key in (
        "minimum_ram",
        "recommended_ram",
        "minimum_vram",
        "recommended_vram",
    ):
        value = requirements.get(key, 0)
        if type(value) is not int or value < 0:
            raise InvalidResourceRequirementsError(
                f"{key} must be a non-negative integer"
            )
        values[key] = value
    return (
        values["minimum_ram"],
        max(values["recommended_ram"], values["minimum_ram"]),
        values["minimum_vram"],
        max(values["recommended_vram"], values["minimum_vram"]),
    )


def _host_architecture() -> str:
    aliases = {"amd64": "x86_64", "arm64": "aarch64", "x64": "x86_64"}
    machine = platform.machine().strip()
    return aliases.get(machine.casefold(), machine or "unknown")


def _host_platform() -> str:
    target = sys.platform.casefold()
    if target.startswith("linux"):
        return "linux"
    if target.startswith("win"):
        return "windows"
    return target or "unknown"


def _provider_vendor(provider: str) -> str | None:
    return {
        "CUDAExecutionProvider": "nvidia",
        "TensorrtExecutionProvider": "nvidia",
        "ROCMExecutionProvider": "amd",
    }.get(provider)


def _hardware_for_backend(
    devices: list[ComputeDevice], backend: ExecutionBackend
) -> list[ComputeDevice]:
    vendor = _provider_vendor(backend.provider)
    if vendor is not None:
        return [
            device
            for device in devices
            if device.kind == "gpu" and device.evidence.get("vendor") == vendor
        ]
    if backend.provider == "DmlExecutionProvider":
        # DirectML 可承载 Windows GPU 与 NPU（Intel AI Boost / Qualcomm Hexagon）
        return [device for device in devices if device.kind in ("gpu", "npu")]
    return []


def _backend_for_hardware(
    backend: ExecutionBackend, hardware: ComputeDevice
) -> ExecutionBackend | None:
    if hardware.evidence.get("identity_persistent") is False:
        return None
    if backend.provider in {
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
        "ROCMExecutionProvider",
        "DmlExecutionProvider",
    }:
        # Windows NPU（ComputeAccelerator PnP 设备）无 CUDA 式设备序号，
        # DirectML 直接承载即可，无需 device_id 绑定。
        if backend.provider == "DmlExecutionProvider" and hardware.kind == "npu":
            return backend
        provider_ordinals = hardware.evidence.get("provider_ordinals")
        if not isinstance(provider_ordinals, Mapping):
            return None
        device_id = provider_ordinals.get(backend.provider)
        if type(device_id) is not int or device_id < 0:
            return None
        return replace(backend, options={**backend.options, "device_id": device_id})
    return backend


def _is_real_gpu(device: ComputeDevice) -> bool:
    return (
        device.kind == "gpu"
        and device.evidence.get("identity_persistent") is not False
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            (key, _canonical_value(item))
            for key, item in sorted(value.items())
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_canonical_value(item) for item in value)
    return value


def _binding_key(backend: ExecutionBackend) -> tuple[Any, ...]:
    return (
        backend.runtime.value,
        backend.provider,
        _canonical_value(backend.options),
    )


class DeviceRegistry:
    def __init__(
        self,
        *,
        ort_module: Any | None = None,
        system_probe: Callable[[], list[ComputeDevice]] = probe_system_devices,
    ) -> None:
        if ort_module is None:
            import onnxruntime as ort_module

        self._provider_probe = OrtProviderProbe(ort_module)
        self._system_probe = system_probe
        self._devices: tuple[ComputeDevice, ...] | None = None
        self._backends: dict[tuple[Any, ...], ExecutionBackend] = {}
        # TTL 缓存：设备探测含 sudo 子进程 + onnxruntime 真实推理验证，单次
        # 可达数秒且曾被观测挂死 18h（manager.refresh_health 注释）。健康环
        # 每 60s 强扫会把这份开销变成常驻负载；默认 TTL 内直接复用上次结果，
        # env DEVICE_SCAN_TTL_SECONDS 覆盖，<=0 表示禁用缓存。
        try:
            self._scan_ttl = float(os.getenv("DEVICE_SCAN_TTL_SECONDS", "300"))
        except ValueError:
            self._scan_ttl = 300.0
        self._scanned_at: float = 0.0

    def scan(self, force: bool = False) -> list[ComputeDevice]:
        # 未强扫且已有结果时：
        # - _devices 为 None（从未扫过）→ 必须探测；
        # - TTL>0 且未过期 → 复用上次结果（健康环高频调用走此分支）；
        # - TTL<=0 或已过期 → 真实重探。
        if self._devices is not None and not force:
            if (
                self._scan_ttl > 0
                and (time.monotonic() - self._scanned_at) < self._scan_ttl
            ):
                return list(self._devices)
        previous_devices = self._devices or ()
        previous_backends = self._backends
        system_devices, available_providers, cpu = self._probe()
        current_providers = set(available_providers)

        verified = self._verify_backends(available_providers, system_devices)
        disappeared = [
            replace(
                backend,
                healthy=False,
                evidence={**backend.evidence, "reason": "provider_disappeared"},
            )
            for backend in previous_backends.values()
            if backend.provider not in current_providers
        ]
        verified.extend(disappeared)
        self._backends = {}
        for backend in verified:
            self._backends[_binding_key(backend)] = backend
        disappeared_providers = {backend.provider for backend in disappeared}
        current_backends = [
            backend
            for backend in verified
            if backend.provider not in disappeared_providers
        ]
        devices = self._attach_backends(system_devices, cpu, current_backends)
        self._preserve_disappeared_provider_backends(devices, previous_devices, disappeared_providers)
        self._preserve_disappeared_device_backends(devices, previous_devices, system_devices, current_providers)
        self._devices = tuple(devices)
        self._scanned_at = time.monotonic()
        return list(self._devices)

    def _probe(self) -> tuple[list[ComputeDevice], tuple[str, ...], ComputeDevice | None]:
        """探测系统设备与可用 provider，补 CPU 兜底设备。"""
        system_devices = list(self._system_probe())
        available_providers = self._provider_probe.list_available()
        available_providers = tuple(
            provider
            for provider in available_providers
            if provider not in _NON_LOCAL_PROVIDERS
        )
        cpu = next((device for device in system_devices if device.kind == "cpu"), None)
        if cpu is None and "CPUExecutionProvider" in set(available_providers):
            cpu = ComputeDevice(
                id="cpu:0",
                name="CPU",
                kind="cpu",
                architecture=_host_architecture(),
                state=DeviceState.AVAILABLE,
                memory_total=0,
                memory_available=0,
                system={"platform": _host_platform()},
                evidence={"source": "onnxruntime"},
            )
            system_devices.append(cpu)
        return system_devices, available_providers, cpu

    def _verify_backends(
        self, available_providers: tuple[str, ...], system_devices: list[ComputeDevice],
    ) -> list[ExecutionBackend]:
        """验证每个 provider 的执行 backend（CPU/Dml/普通 provider 分别处理）。"""
        verified: list[ExecutionBackend] = []
        for provider in available_providers:
            prototype = ExecutionBackend(
                runtime="ort",
                provider=provider,
                healthy=False,
            )
            if provider == "CPUExecutionProvider":
                verified.append(self._provider_probe.verify(provider))
                continue
            vendor = _provider_vendor(provider)
            if vendor is not None or provider == "DmlExecutionProvider":
                bound_backends = []
                for hardware in _hardware_for_backend(system_devices, prototype):
                    bound = _backend_for_hardware(prototype, hardware)
                    if bound is not None:
                        bound_backends.append(
                            self._provider_probe.verify(provider, dict(bound.options))
                        )
                verified.extend(bound_backends)
                if provider == "DmlExecutionProvider" and not bound_backends:
                    verified.append(self._provider_probe.verify(provider))
                continue
            verified.append(self._provider_probe.verify(provider))
        return verified

    def _preserve_disappeared_provider_backends(
        self, devices: list[ComputeDevice], previous_devices: tuple,
        disappeared_providers: set[str],
    ) -> None:
        """保留消失 provider 的 GPU backend（标记 unhealthy 并合并/追加）。in-place 修改 devices。"""
        retained_ids = {device.id for device in devices}
        for device in previous_devices:
            if not _is_real_gpu(device):
                continue
            retained_backends = tuple(
                replace(
                    backend,
                    healthy=False,
                    evidence={**backend.evidence, "reason": "provider_disappeared"},
                )
                for backend in device.backends
                if backend.provider in disappeared_providers
            )
            if retained_backends:
                if device.id in retained_ids:
                    retained_index = next(
                        index
                        for index, current in enumerate(devices)
                        if current.id == device.id
                    )
                    current = devices[retained_index]
                    merged_backends = current.backends + retained_backends
                    devices[retained_index] = replace(
                        current,
                        state=(
                            DeviceState.AVAILABLE
                            if any(backend.healthy for backend in merged_backends)
                            else DeviceState.DEGRADED
                        ),
                        backends=merged_backends,
                    )
                else:
                    devices.append(
                        replace(
                            device,
                            state=DeviceState.UNAVAILABLE,
                            backends=retained_backends,
                        )
                    )
                    retained_ids.add(device.id)

    def _preserve_disappeared_device_backends(
        self, devices: list[ComputeDevice], previous_devices: tuple,
        system_devices: list[ComputeDevice], current_providers: set[str],
    ) -> None:
        """保留消失设备的 GPU backend（标记 unhealthy 并合并/追加）。in-place 修改 devices。"""
        current_hardware_ids = {device.id for device in system_devices}
        for device in previous_devices:
            if not _is_real_gpu(device) or device.id in current_hardware_ids:
                continue
            retained_backends = tuple(
                replace(
                    backend,
                    healthy=False,
                    evidence={**backend.evidence, "reason": "device_disappeared"},
                )
                for backend in device.backends
                if backend.provider in current_providers
            )
            if not retained_backends:
                continue
            retained_index = next(
                (
                    index
                    for index, current in enumerate(devices)
                    if current.id == device.id
                ),
                None,
            )
            if retained_index is None:
                devices.append(
                    replace(
                        device,
                        state=DeviceState.UNAVAILABLE,
                        backends=retained_backends,
                    )
                )
                continue
            current = devices[retained_index]
            existing_bindings = {
                _binding_key(backend)
                for backend in current.backends
            }
            merged_backends = current.backends + tuple(
                backend
                for backend in retained_backends
                if _binding_key(backend) not in existing_bindings
            )
            devices[retained_index] = replace(
                current,
                state=DeviceState.UNAVAILABLE,
                backends=merged_backends,
            )

    @staticmethod
    def _attach_backend_to_device(
        devices: list[ComputeDevice],
        hardware: ComputeDevice,
        bound_backend: ExecutionBackend,
        backend_healthy: bool,
    ) -> None:
        existing_index = next(
            (i for i, d in enumerate(devices) if d.id == hardware.id),
            None,
        )
        attached = replace(
            hardware,
            backends=hardware.backends + (bound_backend,),
            state=DeviceState.AVAILABLE if backend_healthy else DeviceState.DEGRADED,
        )
        if existing_index is None:
            devices.append(attached)
        else:
            merged = devices[existing_index].backends + (bound_backend,)
            devices[existing_index] = replace(
                devices[existing_index],
                state=DeviceState.AVAILABLE if any(b.healthy for b in merged) else DeviceState.DEGRADED,
                backends=merged,
            )

    @staticmethod
    def _make_virtual_device(backend: ExecutionBackend) -> ComputeDevice:
        is_dml = backend.provider == "DmlExecutionProvider"
        return ComputeDevice(
            id="ort:dml:default" if is_dml else _provider_device_id(backend.provider),
            name="DirectML default adapter" if is_dml else backend.provider,
            kind="accelerator",
            architecture=_host_architecture() if is_dml else "unknown",
            state=DeviceState.AVAILABLE if backend.healthy else DeviceState.DEGRADED,
            memory_total=0,
            memory_available=0,
            backends=(backend,),
            system={"platform": "windows" if is_dml else _host_platform()},
            evidence=(
                {"source": "onnxruntime_default_adapter", "identity_persistent": False}
                if is_dml
                else {"source": "onnxruntime"}
            ),
        )

    def _attach_backends(
        self,
        system_devices: list[ComputeDevice],
        cpu: ComputeDevice | None,
        backends: list[ExecutionBackend],
    ) -> list[ComputeDevice]:
        devices: list[ComputeDevice] = []
        attached_ids: set[str] = set()
        for backend in backends:
            if backend.provider == "CPUExecutionProvider" and cpu is not None:
                devices.append(replace(cpu, backends=cpu.backends + (backend,)))
                attached_ids.add(cpu.id)
                continue
            hardware_devices = _hardware_for_backend(system_devices, backend)
            if hardware_devices:
                attached_backend = False
                for hardware in hardware_devices:
                    bound_backend = _backend_for_hardware(backend, hardware)
                    if bound_backend is None or bound_backend.options != backend.options:
                        continue
                    self._attach_backend_to_device(devices, hardware, bound_backend, backend.healthy)
                    attached_ids.add(hardware.id)
                    attached_backend = True
                if attached_backend:
                    continue
            if _provider_vendor(backend.provider) is not None or (
                backend.provider == "DmlExecutionProvider" and backend.options
            ):
                continue
            devices.append(self._make_virtual_device(backend))
        devices.extend(
            device
            for device in system_devices
            if device is not cpu and device.id not in attached_ids
        )
        if cpu is not None and not any(device.id == cpu.id for device in devices):
            devices.append(cpu)
        return devices

    def backend(
        self,
        provider: str,
        device_id: int | None = None,
    ) -> ExecutionBackend:
        if self._devices is None:
            self.scan()
        bindings = [
            item
            for item in self._backends.values()
            if item.provider == provider
        ]
        if not bindings:
            raise IncompatibleBackendError(f"backend is unavailable: {provider}")
        if device_id is None:
            if len(bindings) == 1:
                return bindings[0]
            available_device_ids = tuple(
                binding.options.get("device_id")
                for binding in bindings
                if type(binding.options.get("device_id")) is int
            )
            raise IncompatibleBackendError(
                f"backend is ambiguous: {provider}; "
                f"available device_id values: {available_device_ids}"
            )
        matches = [
            binding
            for binding in bindings
            if type(device_id) is int
            and type(binding.options.get("device_id")) is int
            and binding.options["device_id"] == device_id
        ]
        if len(matches) != 1:
            raise IncompatibleBackendError(
                f"backend is unavailable: {provider} device_id={device_id!r}"
            )
        return matches[0]

    def _collect_candidates(
        self,
        model: CatalogModel,
        minimum_ram: int,
        minimum_vram: int,
        override: str | None,
    ) -> list[tuple[ComputeDevice, ExecutionBackend]]:
        devices = self.scan()
        host_ram_available = max(
            (d.memory_available for d in devices if d.kind == "cpu" and d.state is DeviceState.AVAILABLE),
            default=0,
        )
        candidates = [
            (device, backend)
            for device in devices
            for backend in device.backends
            if self._compatible(model, device, backend, host_ram_available=host_ram_available, minimum_ram=minimum_ram, minimum_vram=minimum_vram)
        ]
        if override is not None:
            candidates = [
                item for item in candidates
                if item[0].id == override and item[0].evidence.get("identity_persistent") is not False
            ]
        return candidates, host_ram_available

    @staticmethod
    def _build_profile_options(
        backend: ExecutionBackend,
        fallback_bindings: list[tuple[ComputeDevice, ExecutionBackend]],
    ) -> dict[str, Any]:
        providers = (backend.provider,) + tuple(b[1].provider for b in fallback_bindings)
        provider_options = (backend.options,) + tuple(b[1].options for b in fallback_bindings)
        fallback_providers = tuple(dict.fromkeys(b[1].provider for b in fallback_bindings))
        options = dict(backend.options)
        if fallback_bindings:
            options["fallback_bindings"] = tuple(
                {"device_id": fd.id, "provider": fb.provider, "provider_options": fb.options}
                for fd, fb in fallback_bindings
            )
        if fallback_providers:
            options["fallback_providers"] = fallback_providers
        if len(providers) > 1:
            options["providers"] = providers
            options["provider_options"] = provider_options
        return options

    def recommend(
        self,
        model: CatalogModel,
        override: str | None = None,
    ) -> RuntimeProfile:
        minimum_ram, recommended_ram, minimum_vram, recommended_vram = _resource_requirements(model)
        candidates, host_ram_available = self._collect_candidates(model, minimum_ram, minimum_vram, override)
        if not candidates:
            target = f" for override {override}" if override is not None else ""
            raise IncompatibleBackendError(f"no compatible backend{target}")
        candidates.sort(
            key=lambda item: (
                host_ram_available < recommended_ram,
                self._vram_available(item[0]) < recommended_vram,
                provider_rank(item[1].provider),
                -item[0].memory_available,
            )
        )
        device, backend = candidates[0]
        fallback_bindings = self._fallback_bindings(
            model, device, backend, candidates,
            host_ram_available=host_ram_available, minimum_ram=minimum_ram, minimum_vram=minimum_vram,
        )
        options = self._build_profile_options(backend, fallback_bindings)
        runtime = backend.runtime
        model_runtimes = _allowed(model.compatibility, "runtimes", "runtime")
        if runtime == "ort" and "ort_genai" in model_runtimes:
            runtime = "ort_genai"
        return RuntimeProfile(
            runtime=runtime,
            device_id=device.id,
            provider=backend.provider,
            options=options,
            estimated_ram=minimum_ram,
            estimated_vram=minimum_vram,
            allow_fallback=len(options.get("providers", ())) > 1,
        )

    @staticmethod
    def _vram_available(device: ComputeDevice) -> int:
        return device.memory_available if device.kind in {"gpu", "accelerator"} else 0

    @staticmethod
    def _fallback_providers(model: CatalogModel, provider: str) -> tuple[str, ...]:
        providers = _allowed(
            model.compatibility,
            "providers",
            "execution_providers",
        )
        try:
            selected_index = providers.index(provider)
        except ValueError:
            return ()
        return providers[selected_index + 1 :]

    def _fallback_bindings(
        self,
        model: CatalogModel,
        selected_device: ComputeDevice,
        backend: ExecutionBackend,
        candidates: list[tuple[ComputeDevice, ExecutionBackend]],
        *,
        host_ram_available: int,
        minimum_ram: int,
        minimum_vram: int,
    ) -> tuple[tuple[ComputeDevice, ExecutionBackend], ...]:
        provider_order = (
            backend.provider,
            *self._fallback_providers(model, backend.provider),
        )
        ordered: list[tuple[ComputeDevice, ExecutionBackend]] = []
        for provider in provider_order:
            matching = [
                (candidate_device, candidate_backend)
                for candidate_device, candidate_backend in candidates
                if candidate_backend.provider == provider
                and not (
                    candidate_device.id == selected_device.id
                    and candidate_backend == backend
                )
                and self._compatible(
                    model,
                    candidate_device,
                    candidate_backend,
                    host_ram_available=host_ram_available,
                    minimum_ram=minimum_ram,
                    minimum_vram=minimum_vram,
                )
            ]
            matching.sort(key=lambda item: -item[0].memory_available)
            ordered.extend(matching)
        return tuple(ordered)

    @staticmethod
    def _compatible(
        model: CatalogModel,
        device: ComputeDevice,
        backend: ExecutionBackend,
        *,
        host_ram_available: int,
        minimum_ram: int,
        minimum_vram: int,
    ) -> bool:
        if not backend.healthy or device.state is not DeviceState.AVAILABLE:
            return False
        compatibility = model.compatibility
        architectures = _allowed(compatibility, "architectures", "architecture")
        providers = _allowed(compatibility, "providers", "execution_providers")
        runtimes = _allowed(compatibility, "runtimes", "runtime")
        purposes = _allowed(compatibility, "purposes", "purpose")
        precisions = _allowed(compatibility, "precisions", "precision")
        platform = device.system.get("platform")
        platforms = _allowed(compatibility, "platforms", "os")
        architecture = (
            _host_architecture()
            if device.architecture == "unknown" and backend.runtime.value.startswith("ort")
            else device.architecture
        )
        if architectures and architecture not in architectures:
            return False
        if providers and backend.provider not in providers:
            return False
        if runtimes:
            # CPU 可承载 ORT GenAI 模型：CPUExecutionProvider 能加载 ort_genai 运行时，
            # 只是 runtime 标识不同（ORT 设备扫描只注册 ort runtime backend）。
            ort_genai_on_cpu = (
                "ort_genai" in runtimes
                and backend.provider == "CPUExecutionProvider"
            )
            if not ort_genai_on_cpu and backend.runtime.value not in runtimes:
                return False
        if purposes and model.purpose.value not in purposes:
            return False
        if backend.purposes and model.purpose not in backend.purposes:
            return False
        if platforms and platform not in platforms:
            return False
        if precisions and model.quantization and model.quantization not in precisions:
            return False
        if backend.precisions and model.quantization and model.quantization not in backend.precisions:
            return False
        return (
            host_ram_available >= minimum_ram
            and DeviceRegistry._vram_available(device) >= minimum_vram
        )
