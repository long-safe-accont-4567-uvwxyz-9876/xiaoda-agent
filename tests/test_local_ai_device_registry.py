import hashlib
import platform
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_ai.contracts import (
    CatalogModel,
    ComputeDevice,
    DeviceState,
    ExecutionBackend,
    ModelPurpose,
    RuntimeKind,
    RuntimeProfile,
)
from local_ai.devices import system_probe
from local_ai.devices.ort_providers import OrtProviderProbe
from local_ai.devices.registry import (
    DeviceRegistry,
    IncompatibleBackendError,
    InvalidResourceRequirementsError,
    _backend_for_hardware,
    _binding_key,
)
from memory import local_embed

# local_embed._create_session 会对单 CUDA EP 自动注入默认 provider_options
# （session_tuning.auto_provider_options：cudnn_conv_algo_search=HEURISTIC，
# 比 ORT 默认 EXHAUSTIVE 建图更快）；显式传入的键（device_id 等）保留。
_CUDA_AUTO_OPTS = {"cudnn_conv_algo_search": "HEURISTIC"}


def _read_text_lookup(values):
    """按 posix 路径查 mock 值：Windows 上 Path 的 str() 用反斜杠，
    而测试 dict 用正斜杠 key，直接 str(path) 会全部 miss。"""

    def read_text(path):
        return values.get(Path(path).as_posix(), "")

    return read_text


def host_architecture():
    aliases = {"amd64": "x86_64", "arm64": "aarch64", "x64": "x86_64"}
    machine = platform.machine().strip()
    return aliases.get(machine.casefold(), machine or "unknown")


def host_platform():
    target = platform.system().casefold()
    return {"linux": "linux", "windows": "windows"}.get(
        target, target or "unknown"
    )


class FakeOrt:
    def __init__(self):
        self.available = ["CPUExecutionProvider"]
        self.fail_session_for = set()
        self.fail_session_bindings = set()
        self.fail_run_for = set()
        self.active_provider_for = {}
        self.sessions = []
        self.runs = []
        self.session_config_entries = []
        self.session_options = []
        self.ORT_SEQUENTIAL = "ORT_SEQUENTIAL"

    def get_available_providers(self):
        return list(self.available)

    def SessionOptions(self):
        fake_ort = self

        class FakeSessionOptions:
            def __init__(self):
                self.enable_mem_pattern = True
                self.execution_mode = None

            def add_session_config_entry(self, key, value):
                fake_ort.session_config_entries.append((key, value))

        options = FakeSessionOptions()
        self.session_options.append(options)
        return options

    def InferenceSession(self, model, *, providers, provider_options=None, sess_options=None):
        provider = providers[0]
        self.sessions.append((provider, provider_options))
        options = provider_options[0] if provider_options else {}
        if provider in self.fail_session_for or (
            provider,
            options.get("device_id"),
        ) in self.fail_session_bindings:
            raise RuntimeError(f"cannot initialize {provider}")
        active_provider = self.active_provider_for.get(provider, provider)
        fake_ort = self

        class FakeSession:
            def get_providers(self):
                return [active_provider]

            def run(self, output_names, feeds):
                fake_ort.runs.append((provider, output_names, feeds))
                if provider in fake_ort.fail_run_for:
                    raise RuntimeError(f"cannot run {provider}")
                return [feeds["input"]]

        return FakeSession()


class FakeLocalSessionOptions:
    def __init__(self):
        self.entries = []

    def add_session_config_entry(self, key, value):
        self.entries.append((key, value))


def cpu_device():
    return ComputeDevice(
        id="cpu:0",
        name="CPU",
        kind="cpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=16_000,
        memory_available=12_000,
        system={"platform": "linux"},
    )


def model(*, providers=("CPUExecutionProvider",), architectures=None,
          runtime_requirements=None):
    if architectures is None:
        architectures = (host_architecture(),)
    return CatalogModel(
        id="embedding:test",
        source="modelscope",
        repository="owner/model",
        revision="abcdef0",
        purpose=ModelPurpose.EMBEDDING,
        files=({"path": "model.onnx", "size": 1, "sha256": "a" * 64},),
        compatibility={
            "architectures": list(architectures),
            "providers": list(providers),
        },
        runtime_requirements=runtime_requirements or {},
    )


def registry(fake_ort):
    return DeviceRegistry(ort_module=fake_ort, system_probe=lambda: [cpu_device()])


def test_provider_probe_lists_available_providers_in_runtime_order():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]

    assert OrtProviderProbe(fake_ort).list_available() == (
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    )


def test_unhealthy_available_provider_is_not_selectable():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    fake_ort.fail_session_for = {"ROCMExecutionProvider"}
    amd = ComputeDevice(
        id="amd:0",
        name="AMD GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        evidence={
            "vendor": "amd",
            "provider_ordinals": {"ROCMExecutionProvider": 0},
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort, system_probe=lambda: [cpu_device(), amd]
    )

    device_registry.scan(force=True)

    assert device_registry.backend("ROCMExecutionProvider").healthy is False
    assert device_registry.backend("CPUExecutionProvider").healthy is True
    assert [call[0] for call in fake_ort.sessions] == [
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_provider_probe_rejects_silent_cpu_fallback():
    fake_ort = FakeOrt()
    fake_ort.active_provider_for["ROCMExecutionProvider"] = "CPUExecutionProvider"

    backend = OrtProviderProbe(fake_ort).verify("ROCMExecutionProvider")

    assert backend.healthy is False
    assert backend.evidence["active_providers"] == ("CPUExecutionProvider",)
    assert fake_ort.runs == []


def test_provider_probe_rejects_first_inference_failure():
    fake_ort = FakeOrt()
    fake_ort.fail_run_for = {"ROCMExecutionProvider"}

    backend = OrtProviderProbe(fake_ort).verify("ROCMExecutionProvider")

    assert backend.healthy is False
    assert "cannot run ROCMExecutionProvider" in backend.evidence["error"]
    assert [call[0] for call in fake_ort.runs] == ["ROCMExecutionProvider"]


def test_provider_probe_disables_cpu_fallback_for_accelerators():
    fake_ort = FakeOrt()

    OrtProviderProbe(fake_ort).verify("ROCMExecutionProvider")
    OrtProviderProbe(fake_ort).verify("CPUExecutionProvider")

    assert fake_ort.session_config_entries == [
        ("session.disable_cpu_ep_fallback", "1")
    ]


def test_provider_probe_passes_requested_provider_options_to_session():
    fake_ort = FakeOrt()

    backend = OrtProviderProbe(fake_ort).verify(
        "CUDAExecutionProvider", {"device_id": 7}
    )

    assert backend.healthy is True
    assert backend.options == {"device_id": 7}
    assert fake_ort.sessions == [
        ("CUDAExecutionProvider", [{"device_id": 7}])
    ]


def test_directml_probe_uses_supported_session_options():
    fake_ort = FakeOrt()

    backend = OrtProviderProbe(fake_ort).verify("DmlExecutionProvider", {"device_id": 3})

    assert backend.healthy is True
    assert fake_ort.session_options[0].enable_mem_pattern is False
    assert fake_ort.session_options[0].execution_mode == fake_ort.ORT_SEQUENTIAL


def test_linux_nvidia_probe_records_provider_ordinals(monkeypatch):
    monkeypatch.setattr(
        system_probe,
        "_run_command",
        lambda command: "2, GPU-2, RTX 4090, 24564, 20000, 555.1, 0000:03:00.0",
    )
    # 宿主环境可能设置了 CUDA_VISIBLE_DEVICES（如 conda），会使
    # provider_ordinals 变为空；显式清空保证测试确定。
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)

    gpu = system_probe._linux_nvidia_gpus()[0]

    assert gpu.evidence["provider_ordinals"] == {
        "CUDAExecutionProvider": 2,
        "TensorrtExecutionProvider": 2,
    }


def test_amd_and_windows_cim_gpus_do_not_invent_provider_ordinals(monkeypatch):
    monkeypatch.setattr(system_probe, "_glob_paths", lambda pattern: ["/sys/class/drm/card0"])
    values = {
        "/sys/class/drm/card0/device/vendor": "0x1002",
        "/sys/class/drm/card0/device/device": "0x744c",
        "/sys/class/drm/card0/device/uevent": "PCI_SLOT_NAME=0000:03:00.0\nDRIVER=amdgpu",
        "/sys/class/drm/card0/device/mem_info_vram_total": "8000",
        "/sys/class/drm/card0/device/mem_info_vram_used": "1000",
        "/sys/class/drm/card0/device/product_name": "AMD GPU",
    }
    monkeypatch.setattr(system_probe, "_read_text", _read_text_lookup(values))
    amd = system_probe._linux_amd_gpus()[0]
    cim = system_probe._windows_gpus(
        {
            "video_controllers": [{
                "Name": "NVIDIA GPU",
                "PNPDeviceID": r"PCI\VEN_10DE&DEV_2684&SUBSYS_00000000&REV_A1\4&ABC&0&0008",
            }]
        }
    )[0]

    assert "provider_ordinals" not in amd.evidence
    assert "provider_ordinals" not in cim.evidence


def test_rocm_without_ordinal_retains_amd_drm_device_and_continues_scan(monkeypatch):
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    card = "/sys/class/drm/card0"
    values = {
        f"{card}/device/vendor": "0x1002",
        f"{card}/device/device": "0x744c",
        f"{card}/device/uevent": "PCI_SLOT_NAME=0000:03:00.0\nDRIVER=amdgpu",
        f"{card}/device/mem_info_vram_total": "8000",
        f"{card}/device/mem_info_vram_used": "2000",
        f"{card}/device/product_name": "AMD GPU",
    }
    monkeypatch.setattr(system_probe, "_glob_paths", lambda pattern: [card])
    monkeypatch.setattr(system_probe, "_read_text", _read_text_lookup(values))
    amd = system_probe._linux_amd_gpus()[0]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), amd],
    )

    devices = device_registry.scan(force=True)

    detected = next(device for device in devices if device.id == amd.id)
    assert detected.id == "pci:0000:03:00.0"
    assert detected.state is DeviceState.AVAILABLE
    assert detected.memory_available == 6_000
    assert "provider_ordinals" not in detected.evidence
    assert detected.backends == ()
    assert fake_ort.sessions == [
        ("CPUExecutionProvider", None),
    ]
    assert device_registry.backend("CPUExecutionProvider").healthy is True
    with pytest.raises(IncompatibleBackendError):
        device_registry.backend("ROCMExecutionProvider")


def test_backend_binding_requires_explicit_provider_ordinal():
    fake_ort = FakeOrt()
    backend = OrtProviderProbe(fake_ort).verify("CUDAExecutionProvider")
    hardware = ComputeDevice(
        id="nvidia:without-ordinal",
        name="GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        evidence={"vendor": "nvidia", "adapter_index": 9, "index": "8"},
    )

    assert _backend_for_hardware(backend, hardware) is None


def test_backend_binding_rejects_nonpersistent_hardware_identity():
    fake_ort = FakeOrt()
    backend = OrtProviderProbe(fake_ort).verify("DmlExecutionProvider")
    hardware = ComputeDevice(
        id="windows-gpu-evidence:weak",
        name="GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=0,
        evidence={
            "identity_persistent": False,
            "provider_ordinals": {"DmlExecutionProvider": 0},
        },
    )

    assert _backend_for_hardware(backend, hardware) is None


def test_windows_gpu_id_hashes_full_normalized_pnp_identity():
    first = r"PCI\VEN_10DE&DEV_2684&SUBSYS_00000000&REV_A1\4&ABC&0&0008"
    second = r" pci\ven_10de&dev_2684&subsys_00000000&rev_a1\4&DEF&0&0008 "

    first_id, _, first_persistent = system_probe._windows_gpu_id(first, 0)
    second_id, _, second_persistent = system_probe._windows_gpu_id(second, 1)

    normalized = first.strip().casefold()
    assert first_id == f"pnp:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"
    assert first_id != second_id
    assert first_persistent is True
    assert second_persistent is True


def test_scan_uses_cache_until_force_requests_a_rescan():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    first = device_registry.scan()
    fake_ort.available.insert(0, "ROCMExecutionProvider")
    cached = device_registry.scan()
    rescanned = device_registry.scan(force=True)

    assert cached is not first
    assert [backend.provider for device in cached for backend in device.backends] == [
        "CPUExecutionProvider"
    ]
    assert [backend.provider for device in rescanned for backend in device.backends] == [
        "CPUExecutionProvider"
    ]
    assert [call[0] for call in fake_ort.sessions] == [
        "CPUExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_force_rescan_retains_disappeared_provider_as_unavailable():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    device_registry = registry(fake_ort)
    device_registry.scan(force=True)

    fake_ort.available = ["CPUExecutionProvider"]
    devices = device_registry.scan(force=True)

    assert all(device.id != "ort:rocm" for device in devices)
    with pytest.raises(IncompatibleBackendError):
        device_registry.backend("ROCMExecutionProvider")


def test_force_rescan_retains_real_gpu_id_when_provider_disappears():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-original",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {"CUDAExecutionProvider": 3},
        },
    )
    system_scans = iter(([cpu_device(), gpu], [cpu_device()]))
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: list(next(system_scans)),
    )
    device_registry.scan(force=True)

    fake_ort.available = []
    devices = device_registry.scan(force=True)

    retained = next(device for device in devices if device.id == gpu.id)
    assert retained.state is DeviceState.UNAVAILABLE
    assert retained.backends[0].provider == "CUDAExecutionProvider"
    assert retained.backends[0].options == {"device_id": 3}
    assert all(device.id != "ort:cuda" for device in devices)


def test_force_rescan_marks_detected_gpu_degraded_when_provider_disappears():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-still-detected",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {"CUDAExecutionProvider": 1},
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )
    device_registry.scan(force=True)

    fake_ort.available = []
    devices = device_registry.scan(force=True)

    retained = next(device for device in devices if device.id == gpu.id)
    assert retained.state is DeviceState.DEGRADED
    assert retained.backends[0].provider == "CUDAExecutionProvider"
    assert retained.backends[0].healthy is False


def test_force_rescan_retains_and_recovers_only_missing_real_gpu():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpus = [
        ComputeDevice(
            id=f"nvidia:GPU-{index}",
            name=f"Detected GPU {index}",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=6_000,
            system={"platform": host_platform()},
            evidence={
                "vendor": "nvidia",
                "provider_ordinals": {"CUDAExecutionProvider": index},
            },
        )
        for index in range(2)
    ]
    scans = iter(
        (
            [cpu_device(), *gpus],
            [cpu_device(), gpus[0]],
            [cpu_device(), *gpus],
        )
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: list(next(scans)),
    )

    device_registry.scan(force=True)
    missing = device_registry.scan(force=True)
    recovered = device_registry.scan(force=True)

    retained = next(device for device in missing if device.id == gpus[1].id)
    assert retained.state is DeviceState.UNAVAILABLE
    assert retained.backends[0].options == {"device_id": 1}
    assert retained.backends[0].healthy is False
    assert retained.backends[0].evidence["reason"] == "device_disappeared"
    assert next(device for device in missing if device.id == gpus[0].id).state is DeviceState.AVAILABLE
    restored = [device for device in recovered if device.id == gpus[1].id]
    assert len(restored) == 1
    assert restored[0].state is DeviceState.AVAILABLE
    assert restored[0].backends[0].healthy is True


def test_force_rescan_does_not_retain_default_or_ephemeral_gpu():
    fake_ort = FakeOrt()
    fake_ort.available = ["DmlExecutionProvider"]
    ephemeral = ComputeDevice(
        id="windows-gpu-evidence:ephemeral",
        name="Ephemeral GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=4_000,
        memory_available=0,
        evidence={
            "identity_persistent": False,
            "provider_ordinals": {"DmlExecutionProvider": 0},
        },
    )
    scans = iter(([cpu_device(), ephemeral], [cpu_device()]))
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: list(next(scans)),
    )

    first = device_registry.scan(force=True)
    second = device_registry.scan(force=True)

    assert any(device.id == "ort:dml:default" for device in first)
    assert all(device.id != ephemeral.id for device in second)
    assert [device.id for device in second].count("ort:dml:default") == 1


def test_recommend_rejects_provider_that_disappeared_on_rescan():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider"]
    device_registry = registry(fake_ort)
    device_registry.scan(force=True)

    fake_ort.available = []
    device_registry.scan(force=True)

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(providers=("ROCMExecutionProvider",), architectures=())
        )


def test_recommend_filters_incompatible_and_unhealthy_backends():
    fake_ort = FakeOrt()
    fake_ort.available = [
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    ]
    fake_ort.fail_session_for = {"CUDAExecutionProvider"}
    device_registry = registry(fake_ort)
    device_registry.scan(force=True)

    profile = device_registry.recommend(
        model(providers=("CUDAExecutionProvider", "CPUExecutionProvider"))
    )

    assert profile.device_id == "cpu:0"
    assert profile.provider == "CPUExecutionProvider"


def test_recommend_prefers_compatible_acceleration_over_cpu():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    amd = ComputeDevice(
        id="amd:0", name="AMD GPU", kind="gpu",
        architecture=host_architecture(), state=DeviceState.AVAILABLE,
        memory_total=8_000, memory_available=6_000,
        evidence={"vendor": "amd", "provider_ordinals": {"ROCMExecutionProvider": 0}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort, system_probe=lambda: [cpu_device(), amd]
    )
    device_registry.scan(force=True)

    profile = device_registry.recommend(
        model(
            providers=("CPUExecutionProvider", "ROCMExecutionProvider"),
            architectures=(),
        )
    )

    assert profile.provider == "ROCMExecutionProvider"
    assert profile.device_id == "amd:0"


def test_accelerator_uses_host_architecture_for_compatibility_without_cpu_memory():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider"]
    device_registry = registry(fake_ort)

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(model(providers=("ROCMExecutionProvider",)))


def test_recommended_ram_does_not_misclassify_cpu_memory_as_accelerator_vram():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    amd = ComputeDevice(
        id="amd:0", name="AMD GPU", kind="gpu",
        architecture=host_architecture(), state=DeviceState.AVAILABLE,
        memory_total=0, memory_available=0,
        evidence={"vendor": "amd", "provider_ordinals": {"ROCMExecutionProvider": 0}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort, system_probe=lambda: [cpu_device(), amd]
    )

    profile = device_registry.recommend(
        model(
            providers=("ROCMExecutionProvider", "CPUExecutionProvider"),
            architectures=(),
            runtime_requirements={"recommended_ram": 10_000},
        )
    )

    assert profile.provider == "ROCMExecutionProvider"


def test_runtime_profile_enables_fallback_only_for_manifest_declared_alternative():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    amd = ComputeDevice(
        id="amd:0", name="AMD GPU", kind="gpu",
        architecture=host_architecture(), state=DeviceState.AVAILABLE,
        memory_total=8_000, memory_available=6_000,
        evidence={"vendor": "amd", "provider_ordinals": {"ROCMExecutionProvider": 0}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort, system_probe=lambda: [cpu_device(), amd]
    )

    without_fallback = device_registry.recommend(
        model(providers=("ROCMExecutionProvider",), architectures=())
    )
    with_fallback = device_registry.recommend(
        model(
            providers=("ROCMExecutionProvider", "CPUExecutionProvider"),
            architectures=(),
        )
    )

    assert without_fallback.allow_fallback is False
    assert with_fallback.allow_fallback is True


def test_runtime_profile_lists_manifest_successors_as_fallback_providers():
    fake_ort = FakeOrt()
    fake_ort.available = [
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    ]
    gpu = ComputeDevice(
        id="nvidia:0", name="NVIDIA GPU", kind="gpu",
        architecture=host_architecture(), state=DeviceState.AVAILABLE,
        memory_total=8_000, memory_available=6_000,
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 0}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort, system_probe=lambda: [cpu_device(), gpu]
    )

    profile = device_registry.recommend(
        model(
            providers=(
                "CUDAExecutionProvider",
                "ROCMExecutionProvider",
                "CPUExecutionProvider",
            ),
            architectures=(),
        )
    )

    assert profile.provider == "CUDAExecutionProvider"
    assert profile.options["fallback_providers"] == (
        "CPUExecutionProvider",
    )


def test_cpu_provider_is_modeled_when_system_probe_returns_no_devices():
    fake_ort = FakeOrt()
    device_registry = DeviceRegistry(ort_module=fake_ort, system_probe=lambda: [])

    devices = device_registry.scan(force=True)

    assert len(devices) == 1
    assert devices[0].id == "cpu:0"
    assert devices[0].kind == "cpu"
    assert devices[0].architecture == host_architecture()
    assert devices[0].memory_total == 0
    assert devices[0].memory_available == 0


def test_cpu_device_is_not_synthesized_without_cpu_provider():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider"]
    device_registry = DeviceRegistry(ort_module=fake_ort, system_probe=lambda: [])

    devices = device_registry.scan(force=True)

    assert all(device.kind != "cpu" for device in devices)
    assert devices == []


def test_real_host_architecture_is_accepted_by_cpu_manifest():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    profile = device_registry.recommend(model())

    assert cpu_device().architecture == host_architecture()
    assert profile.device_id == "cpu:0"


def test_unknown_accelerator_does_not_inherit_cpu_resources():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    device_registry = registry(fake_ort)

    devices = device_registry.scan(force=True)

    assert all(device.id != "ort:rocm" for device in devices)


@pytest.mark.parametrize(
    ("provider", "device_id", "vendor", "ordinal"),
    [
        ("CUDAExecutionProvider", "nvidia:GPU-1234", "nvidia", 0),
        ("ROCMExecutionProvider", "pci:0000:03:00.0", "amd", 1),
        ("DmlExecutionProvider", "pci:10de:28e0", "nvidia", 2),
    ],
)
def test_registry_binds_provider_to_matching_hardware_evidence(
    provider, device_id, vendor, ordinal
):
    fake_ort = FakeOrt()
    fake_ort.available = [provider]
    gpu = ComputeDevice(
        id=device_id,
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": "linux"},
        evidence={"vendor": vendor, "provider_ordinals": {provider: ordinal}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    devices = device_registry.scan(force=True)
    detected = next(device for device in devices if device.id == device_id)

    assert detected.name == "Detected GPU"
    assert detected.memory_total == 8_000
    assert detected.memory_available == 6_000
    assert [backend.provider for backend in detected.backends] == [provider]
    assert all(device.id != f"ort:{provider.removesuffix('ExecutionProvider').casefold()}" for device in devices)


def test_directml_without_any_trusted_ordinal_creates_default_adapter():
    fake_ort = FakeOrt()
    fake_ort.available = ["DmlExecutionProvider"]
    weak_gpu = ComputeDevice(
        id="windows-gpu-evidence:weak",
        name="Weak GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=4_000,
        memory_available=0,
        evidence={
            "identity_persistent": False,
            "provider_ordinals": {"DmlExecutionProvider": 0},
        },
    )
    unknown_gpu = ComputeDevice(
        id="pnp:unknown",
        name="Unknown GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=0,
        evidence={"identity_persistent": True},
    )

    devices = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), weak_gpu, unknown_gpu],
    ).scan(force=True)

    default = next(device for device in devices if device.id == "ort:dml:default")
    assert default.name == "DirectML default adapter"
    assert default.kind == "accelerator"
    assert default.architecture == host_architecture()
    assert default.state is DeviceState.AVAILABLE
    assert default.memory_total == 0
    assert default.memory_available == 0
    assert default.system == {"platform": "windows"}
    assert default.evidence == {
        "source": "onnxruntime_default_adapter",
        "identity_persistent": False,
    }
    assert default.backends[0].provider == "DmlExecutionProvider"
    assert default.backends[0].options == {}
    assert fake_ort.sessions == [("DmlExecutionProvider", None)]


def test_directml_with_any_trusted_physical_binding_does_not_create_default():
    fake_ort = FakeOrt()
    fake_ort.available = ["DmlExecutionProvider"]
    trusted_gpu = ComputeDevice(
        id="pnp:trusted",
        name="Trusted GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=0,
        evidence={
            "identity_persistent": True,
            "provider_ordinals": {"DmlExecutionProvider": 2},
        },
    )
    unbound_gpu = ComputeDevice(
        id="pnp:unbound",
        name="Unbound GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=4_000,
        memory_available=0,
        evidence={"identity_persistent": True},
    )

    devices = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), trusted_gpu, unbound_gpu],
    ).scan(force=True)

    assert all(device.id != "ort:dml:default" for device in devices)
    assert fake_ort.sessions == [("DmlExecutionProvider", [{"device_id": 2}])]
    detected = next(device for device in devices if device.id == trusted_gpu.id)
    assert detected.backends[0].options == {"device_id": 2}


def test_registry_exposes_each_matching_gpu_with_provider_device_option():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpus = [
        ComputeDevice(
            id=f"nvidia:GPU-{index}",
            name=f"NVIDIA GPU {index}",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=6_000,
            system={"platform": "linux"},
            evidence={
                "vendor": "nvidia",
                "provider_ordinals": {"CUDAExecutionProvider": index},
            },
        )
        for index in range(2)
    ]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), *gpus],
    )

    devices = device_registry.scan(force=True)

    assert [device.id for device in devices] == ["nvidia:GPU-0", "nvidia:GPU-1", "cpu:0"]
    assert [device.backends[0].options for device in devices[:2]] == [
        {"device_id": 0},
        {"device_id": 1},
    ]
    assert device_registry.recommend(
        model(providers=("CUDAExecutionProvider",), architectures=()),
        override="nvidia:GPU-1",
    ).options == {"device_id": 1}


def test_backend_returns_only_provider_binding_without_device_id():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    assert device_registry.backend("CPUExecutionProvider").provider == (
        "CPUExecutionProvider"
    )


def test_backend_rejects_provider_without_bindings():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    with pytest.raises(IncompatibleBackendError):
        device_registry.backend("CUDAExecutionProvider")


def test_backend_without_device_id_reports_ambiguous_available_ids():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpus = [
        ComputeDevice(
            id=f"nvidia:GPU-{device_id}",
            name=f"NVIDIA GPU {device_id}",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=6_000,
            system={"platform": "linux"},
            evidence={
                "vendor": "nvidia",
                "provider_ordinals": {"CUDAExecutionProvider": device_id},
            },
        )
        for device_id in (0, 1)
    ]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), *gpus],
    )

    with pytest.raises(IncompatibleBackendError) as error:
        device_registry.backend("CUDAExecutionProvider")

    message = str(error.value)
    assert "ambiguous" in message
    assert "device_id" in message
    assert "0" in message
    assert "1" in message


@pytest.mark.parametrize("device_id", ["1", True, 2])
def test_backend_device_id_requires_one_strict_integer_match(device_id):
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-1",
        name="NVIDIA GPU 1",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": "linux"},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {"CUDAExecutionProvider": 1},
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    with pytest.raises(IncompatibleBackendError):
        device_registry.backend("CUDAExecutionProvider", device_id=device_id)


def test_backend_device_id_rejects_duplicate_strict_integer_bindings():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)
    first = ExecutionBackend(
        runtime=RuntimeKind.ORT,
        provider="CUDAExecutionProvider",
        healthy=True,
        options={"device_id": 1, "arena": "first"},
    )
    second = ExecutionBackend(
        runtime=RuntimeKind.ORT,
        provider="CUDAExecutionProvider",
        healthy=True,
        options={"device_id": 1, "arena": "second"},
    )
    device_registry.scan()
    device_registry._backends = {
        _binding_key(first): first,
        _binding_key(second): second,
    }

    with pytest.raises(IncompatibleBackendError):
        device_registry.backend("CUDAExecutionProvider", device_id=1)


def test_cuda_two_cards_query_independent_health_and_recommend_healthy_card():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    fake_ort.fail_session_bindings = {("CUDAExecutionProvider", 0)}
    gpus = [
        ComputeDevice(
            id=f"nvidia:GPU-{device_id}",
            name=f"NVIDIA GPU {device_id}",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=6_000,
            system={"platform": "linux"},
            evidence={
                "vendor": "nvidia",
                "provider_ordinals": {"CUDAExecutionProvider": device_id},
            },
        )
        for device_id in (0, 1)
    ]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), *gpus],
    )

    device_registry.scan(force=True)

    assert device_registry.backend("CUDAExecutionProvider", device_id=0).healthy is False
    assert device_registry.backend("CUDAExecutionProvider", device_id=1).healthy is True
    profile = device_registry.recommend(
        model(providers=("CUDAExecutionProvider",), architectures=())
    )
    assert profile.device_id == "nvidia:GPU-1"
    assert profile.options == {"device_id": 1}


def test_registry_verifies_gpu_provider_once_per_explicit_hardware_mapping():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpus = [
        ComputeDevice(
            id=f"nvidia:GPU-{ordinal}",
            name=f"GPU {ordinal}",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=6_000,
            evidence={
                "vendor": "nvidia",
                "provider_ordinals": {"CUDAExecutionProvider": ordinal},
            },
        )
        for ordinal in (2, 5)
    ]

    devices = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: gpus,
    ).scan(force=True)

    assert fake_ort.sessions == [
        ("CUDAExecutionProvider", [{"device_id": 2}]),
        ("CUDAExecutionProvider", [{"device_id": 5}]),
    ]
    assert [device.backends[0].options for device in devices] == [
        {"device_id": 2},
        {"device_id": 5},
    ]


def test_provider_without_explicit_gpu_mapping_does_not_create_ort_device():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider"]
    amd = ComputeDevice(
        id="pci:0000:03:00.0",
        name="AMD GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        evidence={"vendor": "amd"},
    )

    devices = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [amd],
    ).scan(force=True)

    assert fake_ort.sessions == []
    assert [device.id for device in devices] == [amd.id]
    assert devices[0].backends == ()


def test_unknown_accelerator_resources_fail_positive_memory_requirement():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider"]
    device_registry = registry(fake_ort)
    device_registry.scan(force=True)

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(
                providers=("ROCMExecutionProvider",),
                architectures=(),
                runtime_requirements={"minimum_vram": 1},
            )
        )


def test_manual_override_must_be_model_compatible():
    fake_ort = FakeOrt()
    fake_ort.available = ["ROCMExecutionProvider", "CPUExecutionProvider"]
    device_registry = registry(fake_ort)
    device_registry.scan(force=True)

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(providers=("CPUExecutionProvider",)), override="ort:rocm"
        )


def test_manual_override_rejects_nonpersistent_device_identity():
    fake_ort = FakeOrt()
    fake_ort.available = ["DmlExecutionProvider"]
    weak_gpu = ComputeDevice(
        id="windows-gpu-evidence:weak",
        name="GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=0,
        system={"platform": host_platform()},
        evidence={
            "identity_persistent": False,
            "provider_ordinals": {"DmlExecutionProvider": 0},
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), weak_gpu],
    )

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(providers=("DmlExecutionProvider",), architectures=()),
            override=weak_gpu.id,
        )


def test_directml_default_adapter_is_auto_recommended_only_for_zero_vram():
    fake_ort = FakeOrt()
    fake_ort.available = ["DmlExecutionProvider"]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device()],
    )

    profile = device_registry.recommend(
        model(
            providers=("DmlExecutionProvider",),
            architectures=(),
            runtime_requirements={"minimum_vram": 0},
        )
    )

    assert profile.device_id == "ort:dml:default"

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(
                providers=("DmlExecutionProvider",),
                architectures=(),
                runtime_requirements={"minimum_vram": 1},
            )
        )


def test_directml_default_adapter_rejects_exact_id_override():
    fake_ort = FakeOrt()
    fake_ort.available = ["DmlExecutionProvider"]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device()],
    )

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(providers=("DmlExecutionProvider",), architectures=()),
            override="ort:dml:default",
        )


def test_recommend_rejects_devices_below_manifest_minimum_memory():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)
    device_registry.scan(force=True)

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(runtime_requirements={"minimum_ram": 12_001})
        )


@pytest.mark.parametrize("key", ["minimum_memory", "min_memory", "recommended_memory"])
def test_recommend_rejects_positive_legacy_generic_memory_requirements(key):
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    with pytest.raises(InvalidResourceRequirementsError, match=key):
        device_registry.recommend(model(runtime_requirements={key: 1}))


def test_recommend_ignores_zero_legacy_generic_memory_requirements():
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    profile = device_registry.recommend(
        model(runtime_requirements={"minimum_memory": 0, "recommended_memory": 0})
    )

    assert profile.provider == "CPUExecutionProvider"


@pytest.mark.parametrize(
    ("requirements", "match"),
    [
        ({"minimum_ram": True}, "minimum_ram"),
        ({"minimum_vram": -1}, "minimum_vram"),
        ({"recommended_ram": 1.5}, "recommended_ram"),
    ],
)
def test_recommend_reports_invalid_typed_resource_requirements(requirements, match):
    fake_ort = FakeOrt()
    device_registry = registry(fake_ort)

    with pytest.raises(InvalidResourceRequirementsError, match=match):
        device_registry.recommend(model(runtime_requirements=requirements))


def test_gpu_candidate_checks_host_ram_and_its_own_vram_independently():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-resource",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 0}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    profile = device_registry.recommend(
        model(
            providers=("CUDAExecutionProvider",),
            runtime_requirements={"minimum_ram": 10_000, "minimum_vram": 5_000},
        )
    )

    assert profile.device_id == gpu.id
    assert profile.estimated_ram == 10_000
    assert profile.estimated_vram == 5_000


def test_gpu_candidate_fails_when_host_ram_is_below_typed_requirement():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-resource",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=32_000,
        memory_available=30_000,
        system={"platform": host_platform()},
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 0}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    with pytest.raises(IncompatibleBackendError):
        device_registry.recommend(
            model(
                providers=("CUDAExecutionProvider",),
                runtime_requirements={"minimum_ram": 12_001, "minimum_vram": 1},
            )
        )


def test_rescan_keeps_device_available_when_peer_provider_disappears():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "TensorrtExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-shared",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {
                "CUDAExecutionProvider": 0,
                "TensorrtExecutionProvider": 0,
            },
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )
    device_registry.scan(force=True)

    fake_ort.available = ["CUDAExecutionProvider"]
    devices = device_registry.scan(force=True)

    retained = next(device for device in devices if device.id == gpu.id)
    assert retained.state is DeviceState.AVAILABLE
    assert [backend.provider for backend in retained.backends] == [
        "CUDAExecutionProvider",
        "TensorrtExecutionProvider",
    ]
    assert retained.backends[0].healthy is True
    assert retained.backends[1].healthy is False
    assert device_registry.recommend(
        model(providers=("CUDAExecutionProvider",))
    ).provider == "CUDAExecutionProvider"


def test_shared_hardware_is_available_when_any_attached_backend_is_healthy():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "TensorrtExecutionProvider"]
    fake_ort.fail_session_for = {"CUDAExecutionProvider"}
    gpu = ComputeDevice(
        id="nvidia:GPU-shared",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {
                "CUDAExecutionProvider": 0,
                "TensorrtExecutionProvider": 0,
            },
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    devices = device_registry.scan(force=True)

    detected = next(device for device in devices if device.id == gpu.id)
    assert detected.state is DeviceState.AVAILABLE
    assert device_registry.recommend(
        model(providers=("TensorrtExecutionProvider",))
    ).provider == "TensorrtExecutionProvider"


@pytest.mark.parametrize("refresh", [False, True])
def test_scan_recomputes_degraded_hardware_from_healthy_backend(refresh):
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    state = DeviceState.AVAILABLE if refresh else DeviceState.DEGRADED

    def detected_gpu():
        return ComputeDevice(
            id="nvidia:GPU-recovered",
            name="Detected GPU",
            kind="gpu",
            architecture=host_architecture(),
            state=state,
            memory_total=8_000,
            memory_available=6_000,
            system={"platform": host_platform()},
            evidence={
                "vendor": "nvidia",
                "provider_ordinals": {"CUDAExecutionProvider": 0},
            },
        )

    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), detected_gpu()],
    )
    if refresh:
        device_registry.scan()
        state = DeviceState.DEGRADED

    devices = device_registry.scan(force=refresh)

    detected = next(device for device in devices if device.id == "nvidia:GPU-recovered")
    assert detected.state is DeviceState.AVAILABLE
    assert detected.backends[0].healthy is True


def test_shared_hardware_is_degraded_when_all_attached_backends_fail():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "TensorrtExecutionProvider"]
    fake_ort.fail_session_for = set(fake_ort.available)
    gpu = ComputeDevice(
        id="nvidia:GPU-shared",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {
                "CUDAExecutionProvider": 0,
                "TensorrtExecutionProvider": 0,
            },
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    devices = device_registry.scan(force=True)

    detected = next(device for device in devices if device.id == gpu.id)
    assert detected.state is DeviceState.DEGRADED


def test_binding_key_canonicalizes_nested_provider_options():
    first = OrtProviderProbe(FakeOrt()).verify(
        "CUDAExecutionProvider",
        {"device_id": 0, "tunable": {"ops": ["gemm", "conv"], "enabled": True}},
    )
    second = OrtProviderProbe(FakeOrt()).verify(
        "CUDAExecutionProvider",
        {"tunable": {"enabled": True, "ops": ["gemm", "conv"]}, "device_id": 0},
    )

    assert _binding_key(first) == _binding_key(second)


def test_recommend_uses_healthy_binding_on_available_shared_hardware():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "TensorrtExecutionProvider"]
    fake_ort.fail_session_for = {"CUDAExecutionProvider"}
    gpu = ComputeDevice(
        id="nvidia:GPU-shared",
        name="Detected GPU",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={
            "vendor": "nvidia",
            "provider_ordinals": {
                "CUDAExecutionProvider": 0,
                "TensorrtExecutionProvider": 0,
            },
        },
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    profile = device_registry.recommend(
        model(
            providers=("CUDAExecutionProvider", "TensorrtExecutionProvider"),
            architectures=(),
        )
    )

    assert profile.device_id == gpu.id
    assert profile.provider == "TensorrtExecutionProvider"


def test_local_embedding_forwards_registry_provider_configuration(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_calls = []

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append((path, kwargs))
            self.providers = kwargs["providers"]
            self.providers = kwargs["providers"]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return ["ROCMExecutionProvider", "CPUExecutionProvider"]

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=lambda: object()),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    provider_options = [{"device_id": 0}, {}]

    provider = local_embed.LocalEmbeddingProvider(
        tmp_path,
        providers=["ROCMExecutionProvider", "CPUExecutionProvider"],
        provider_options=provider_options,
    )

    assert provider.load() is True
    assert session_calls[0][1]["providers"] == [
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    ]
    assert session_calls[0][1]["provider_options"] == provider_options


def test_legacy_local_embedding_disable_fallback_requires_exact_provider_chain(
    monkeypatch, tmp_path
):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_options = []

    class FakeSessionOptions:
        def __init__(self):
            self.entries = []
            session_options.append(self)

        def add_session_config_entry(self, key, value):
            self.entries.append((key, value))

    class FakeSession:
        def __init__(self, path, **kwargs):
            self.providers = kwargs["providers"]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return [*self.providers, "CPUExecutionProvider"]

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    provider = local_embed.LocalEmbeddingProvider(
        tmp_path,
        providers=["ROCMExecutionProvider"],
        provider_options=[{"device_id": 0}],
        disable_fallback=True,
    )

    assert provider.load() is False
    assert session_options[0].entries == [
        ("session.disable_cpu_ep_fallback", "1")
    ]


def test_hardware_binding_uses_runtime_adapter_index_instead_of_probe_order():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider"]
    first = ComputeDevice(
        id="nvidia:GPU-7",
        name="GPU 7",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 7}},
    )
    second = ComputeDevice(
        id="nvidia:GPU-2",
        name="GPU 2",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 2}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [second, first],
    )

    devices = device_registry.scan(force=True)

    assert {
        device.id: device.backends[0].options["device_id"] for device in devices
    } == {"nvidia:GPU-2": 2, "nvidia:GPU-7": 7}


def test_runtime_profile_contains_only_executable_ordered_provider_chain():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-4",
        name="GPU 4",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 4}},
    )
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    )

    profile = device_registry.recommend(
        model(
            providers=(
                "CUDAExecutionProvider",
                "ROCMExecutionProvider",
                "CPUExecutionProvider",
            ),
            architectures=(),
        )
    )

    assert profile.options["providers"] == (
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    )
    assert profile.options["provider_options"] == ({"device_id": 4}, {})
    assert profile.allow_fallback is True


def test_runtime_profile_fallback_bindings_are_ordered_executable_candidates():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "ROCMExecutionProvider", "CPUExecutionProvider"]
    fake_ort.fail_session_for = {"ROCMExecutionProvider"}
    gpus = [
        ComputeDevice(
            id=f"nvidia:GPU-{ordinal}",
            name=f"GPU {ordinal}",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=available,
            system={"platform": host_platform()},
            evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": ordinal}},
        )
        for ordinal, available in ((2, 6_000), (7, 5_000))
    ]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), *gpus],
    )

    profile = device_registry.recommend(
        model(
            providers=(
                "CUDAExecutionProvider",
                "ROCMExecutionProvider",
                "CPUExecutionProvider",
            ),
            architectures=(),
            runtime_requirements={"minimum_ram": 1_000, "minimum_vram": 4_000},
        )
    )

    assert profile.device_id == "nvidia:GPU-2"
    assert profile.options["fallback_bindings"] == (
        {
            "device_id": "nvidia:GPU-7",
            "provider": "CUDAExecutionProvider",
            "provider_options": {"device_id": 7},
        },
    )


def test_runtime_profile_fallback_bindings_exclude_unhealthy_incompatible_and_small_devices():
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "ROCMExecutionProvider", "CPUExecutionProvider"]
    fake_ort.fail_session_for = {"ROCMExecutionProvider"}
    devices = [
        ComputeDevice(
            id="nvidia:primary",
            name="Primary GPU",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=6_000,
            system={"platform": host_platform()},
            evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 0}},
        ),
        ComputeDevice(
            id="nvidia:small",
            name="Small GPU",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=3_000,
            memory_available=2_000,
            system={"platform": host_platform()},
            evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 1}},
        ),
        ComputeDevice(
            id="nvidia:wrong-platform",
            name="Wrong Platform GPU",
            kind="gpu",
            architecture=host_architecture(),
            state=DeviceState.AVAILABLE,
            memory_total=8_000,
            memory_available=5_000,
            system={"platform": "windows" if host_platform() != "windows" else "linux"},
            evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 2}},
        ),
    ]
    device_registry = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), *devices],
    )
    manifest = model(
        providers=(
            "CUDAExecutionProvider",
            "ROCMExecutionProvider",
            "CPUExecutionProvider",
        ),
        architectures=(),
        runtime_requirements={"minimum_vram": 4_000},
    )
    manifest = CatalogModel.from_dict(
        {
            **manifest.to_dict(),
            "compatibility": {
                **manifest.to_dict()["compatibility"],
                "platforms": [host_platform()],
            },
        }
    )

    profile = device_registry.recommend(manifest)

    assert profile.options.get("fallback_bindings", ()) == ()
    assert profile.allow_fallback is False


def test_local_embedding_consumes_runtime_profile_provider_chain(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_calls = []

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append((path, kwargs))
            self.providers = kwargs["providers"]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return self.providers

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeLocalSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    fake_ort = FakeOrt()
    fake_ort.available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    gpu = ComputeDevice(
        id="nvidia:GPU-3",
        name="GPU 3",
        kind="gpu",
        architecture=host_architecture(),
        state=DeviceState.AVAILABLE,
        memory_total=8_000,
        memory_available=6_000,
        system={"platform": host_platform()},
        evidence={"vendor": "nvidia", "provider_ordinals": {"CUDAExecutionProvider": 3}},
    )
    profile = DeviceRegistry(
        ort_module=fake_ort,
        system_probe=lambda: [cpu_device(), gpu],
    ).recommend(
        model(
            providers=("CUDAExecutionProvider", "CPUExecutionProvider"),
            architectures=(),
        )
    )

    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(
        tmp_path,
        profile,
    )

    assert provider.load() is True
    assert [call[1]["providers"] for call in session_calls] == [
        ["CUDAExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert [call[1]["provider_options"] for call in session_calls] == [
        [{**_CUDA_AUTO_OPTS, "device_id": 3}],
        [{}],
    ]


def test_local_embedding_builds_provider_chain_from_fallback_bindings(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_calls = []

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append((path, kwargs))
            self.providers = kwargs["providers"]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return self.providers

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeLocalSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="nvidia:GPU-2",
        provider="CUDAExecutionProvider",
        options={
            "device_id": 2,
            "fallback_bindings": (
                {
                    "device_id": "nvidia:GPU-7",
                    "provider": "CUDAExecutionProvider",
                    "provider_options": {"device_id": 7},
                },
                {
                    "device_id": "cpu:0",
                    "provider": "CPUExecutionProvider",
                    "provider_options": {},
                },
            ),
        },
    )

    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(tmp_path, profile)

    assert provider.load() is True
    assert [call[1]["providers"] for call in session_calls] == [
        ["CUDAExecutionProvider"],
        ["CUDAExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert [call[1]["provider_options"] for call in session_calls] == [
        [{**_CUDA_AUTO_OPTS, "device_id": 2}],
        [{**_CUDA_AUTO_OPTS, "device_id": 7}],
        [{}],
    ]


def test_local_embedding_creates_one_session_per_manifest_binding(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_calls = []

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append((path, kwargs))
            self.providers = kwargs["providers"]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return self.providers

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeLocalSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="nvidia:0",
        provider="CUDAExecutionProvider",
        options={
            "device_id": 0,
            "fallback_bindings": (
                {
                    "device_id": "nvidia:1",
                    "provider": "CUDAExecutionProvider",
                    "provider_options": {"device_id": 1},
                },
                {
                    "device_id": "cpu:0",
                    "provider": "CPUExecutionProvider",
                    "provider_options": {},
                },
            ),
        },
    )

    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(tmp_path, profile)

    assert provider.load() is True
    assert [call[1]["providers"] for call in session_calls] == [
        ["CUDAExecutionProvider"],
        ["CUDAExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
    assert [call[1]["provider_options"] for call in session_calls] == [
        [{**_CUDA_AUTO_OPTS, "device_id": 0}],
        [{**_CUDA_AUTO_OPTS, "device_id": 1}],
        [{}],
    ]


def test_local_embedding_load_is_transactional_and_retry_does_not_duplicate_sessions(
    monkeypatch, tmp_path
):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_calls = []
    tokenizer_calls = []

    class FakeSessionOptions:
        def __init__(self):
            self.entries = []

        def add_session_config_entry(self, key, value):
            self.entries.append((key, value))

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append(kwargs)
            self.provider = kwargs["providers"][0]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return [self.provider]

    def load_tokenizer(path):
        tokenizer_calls.append(path)
        if len(tokenizer_calls) == 1:
            raise RuntimeError("tokenizer failed")
        return object()

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=load_tokenizer),
    )
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="nvidia:0",
        provider="CUDAExecutionProvider",
        options={"device_id": 0},
    )

    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(tmp_path, profile)

    assert provider.load() is False
    assert provider.ready is False
    assert provider.active_binding is None
    assert provider.dimensions == 0
    assert provider._session is None
    assert provider._sessions == []
    assert provider._tokenizer is None
    assert provider._active_session_index == 0

    assert provider.load() is True
    assert provider.ready is True
    assert provider.active_binding["device_id"] == "nvidia:0"
    assert provider.dimensions == 512
    assert len(session_calls) == 2


def test_local_embedding_retries_next_session_and_promotes_successful_binding(
    monkeypatch, tmp_path
):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    run_order = []

    class FakeSession:
        def __init__(self, path, **kwargs):
            self.device_id = kwargs["provider_options"][0].get("device_id", "cpu")
            self.providers = kwargs["providers"]

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 2))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return self.providers

        def run(self, output_names, feeds):
            run_order.append(self.device_id)
            if self.device_id == 0:
                raise RuntimeError("primary failed")
            return [local_embed.np.ones((1, 1, 2), dtype=local_embed.np.float32)]

    class FakeEncoding:
        ids = [1]
        type_ids = [0]

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeLocalSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(
            from_file=lambda path: SimpleNamespace(
                encode_batch=lambda texts, add_special_tokens: [FakeEncoding()]
            )
        ),
    )
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="nvidia:0",
        provider="CUDAExecutionProvider",
        options={
            "device_id": 0,
            "fallback_bindings": (
                {
                    "device_id": "nvidia:1",
                    "provider": "CUDAExecutionProvider",
                    "provider_options": {"device_id": 1},
                },
            ),
        },
    )
    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(tmp_path, profile)

    assert provider.embed("first")
    assert provider.embed("second")
    assert run_order == [0, 1, 1]
    assert provider.active_binding["device_id"] == "nvidia:1"


def test_local_embedding_ignores_non_manifest_provider_chain(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_calls = []

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append(kwargs)

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return ["CUDAExecutionProvider"]

    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(
        local_embed,
        "ort",
        SimpleNamespace(InferenceSession=FakeSession, SessionOptions=FakeLocalSessionOptions),
    )
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="nvidia:0",
        provider="CUDAExecutionProvider",
        options={
            "device_id": 0,
            "providers": ("CUDAExecutionProvider", "CPUExecutionProvider"),
            "provider_options": ({"device_id": 0}, {}),
            "fallback_providers": ("CPUExecutionProvider",),
        },
    )

    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(tmp_path, profile)

    assert provider.load() is True
    assert len(session_calls) == 1
    assert session_calls[0]["providers"] == ["CUDAExecutionProvider"]
    assert session_calls[0]["provider_options"] == [{**_CUDA_AUTO_OPTS, "device_id": 0}]


def test_local_embedding_directml_load_uses_supported_session_options(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    session_options = []
    session_calls = []

    class FakeSessionOptions:
        def __init__(self):
            self.enable_mem_pattern = True
            self.execution_mode = None
            self.entries = []
            session_options.append(self)

        def add_session_config_entry(self, key, value):
            self.entries.append((key, value))

    class FakeSession:
        def __init__(self, path, **kwargs):
            session_calls.append((path, kwargs))

        def get_outputs(self):
            return [SimpleNamespace(shape=(None, None, 512))]

        def get_inputs(self):
            return [SimpleNamespace(name="input_ids", type="tensor(int64)", shape=(None, None))]

        def get_providers(self):
            return ["DmlExecutionProvider"]

    fake_ort = SimpleNamespace(
        InferenceSession=FakeSession,
        SessionOptions=FakeSessionOptions,
        ORT_SEQUENTIAL="ORT_SEQUENTIAL",
    )
    monkeypatch.setattr(local_embed, "HAS_LOCAL_EMBED_DEPS", True)
    monkeypatch.setattr(local_embed, "ort", fake_ort)
    monkeypatch.setattr(
        local_embed,
        "Tokenizer",
        SimpleNamespace(from_file=lambda path: object()),
    )
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="windows:gpu:0",
        provider="DmlExecutionProvider",
        options={"device_id": 0},
        allow_fallback=False,
    )

    provider = local_embed.LocalEmbeddingProvider.from_runtime_profile(tmp_path, profile)

    assert provider.load() is True
    assert session_calls[0][1]["sess_options"] is session_options[0]
    assert session_options[0].enable_mem_pattern is False
    assert session_options[0].execution_mode == fake_ort.ORT_SEQUENTIAL
