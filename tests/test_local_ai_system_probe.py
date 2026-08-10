import hashlib
import json
import subprocess
from types import SimpleNamespace

from local_ai.contracts import DeviceState, RuntimeKind
from local_ai.devices import system_probe
from local_ai.devices.vip_probe import parse_vip_probe, probe_vip_backend


def test_linux_arm_probe_uses_device_tree_model(monkeypatch):
    def read_text(path):
        values = {
            "/sys/firmware/devicetree/base/model": "Orange Pi 5 Plus\x00",
            "/proc/cpuinfo": "Hardware\t: Rockchip RK3588\n",
            "/proc/meminfo": "MemTotal: 8192 kB\nMemAvailable: 6144 kB\n",
        }
        return values.get(str(path), "")

    monkeypatch.setattr(system_probe, "_read_text", read_text)
    monkeypatch.setattr(system_probe.platform_module, "machine", lambda: "aarch64")

    devices = system_probe.probe_system_devices("linux")

    cpu = next(device for device in devices if device.kind == "cpu")
    assert cpu.architecture == "aarch64"
    assert cpu.name == "Orange Pi 5 Plus"
    assert cpu.memory_total == 8192 * 1024
    assert cpu.memory_available == 6144 * 1024
    assert cpu.evidence == {
        "device_tree_model": "Orange Pi 5 Plus",
        "cpuinfo_hardware": "Rockchip RK3588",
    }


def test_linux_probe_does_not_infer_board_model_from_architecture(monkeypatch):
    monkeypatch.setattr(system_probe, "_read_text", lambda path: "")
    monkeypatch.setattr(system_probe.platform_module, "machine", lambda: "aarch64")

    cpu = system_probe.probe_system_devices("linux")[0]

    assert cpu.name == "CPU"
    assert "Orange Pi" not in json.dumps(cpu.to_dict())


def test_linux_arm64_alias_is_reported_as_aarch64(monkeypatch):
    monkeypatch.setattr(system_probe, "_read_text", lambda path: "")
    monkeypatch.setattr(system_probe.platform_module, "machine", lambda: "ARM64")

    cpu = system_probe.probe_system_devices("linux")[0]

    assert cpu.architecture == "aarch64"
    assert cpu.name == "CPU"


def test_windows_probe_uses_command_evidence(monkeypatch):
    payload = json.dumps(
        {
            "Name": "AMD Ryzen 7 7840U",
            "Architecture": 9,
            "TotalPhysicalMemory": 17179869184,
            "FreePhysicalMemory": 8388608,
        }
    )
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: payload)

    cpu = system_probe.probe_system_devices("win32")[0]

    assert cpu.name == "AMD Ryzen 7 7840U"
    assert cpu.architecture == "x86_64"
    assert cpu.memory_total == 17179869184
    assert cpu.memory_available == 8388608 * 1024
    assert cpu.evidence["windows_processor_name"] == "AMD Ryzen 7 7840U"


def test_windows_arm_probe_uses_cim_architecture_without_inventing_model(monkeypatch):
    payload = json.dumps(
        {
            "Name": "",
            "Architecture": 12,
            "TotalPhysicalMemory": 8589934592,
            "FreePhysicalMemory": 4194304,
        }
    )
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: payload)
    monkeypatch.setattr(system_probe.platform_module, "machine", lambda: "AMD64")

    cpu = system_probe.probe_system_devices("win32")[0]

    assert cpu.architecture == "aarch64"
    assert cpu.name == "CPU"
    assert "Snapdragon" not in json.dumps(cpu.to_dict())


def test_windows_probe_uses_cim_video_controllers(monkeypatch):
    payload = json.dumps(
        {
            "cpu": {
                "Name": "AMD Ryzen 7 7840U",
                "Architecture": 9,
                "TotalPhysicalMemory": 17179869184,
                "FreePhysicalMemory": 8388608,
            },
            "video_controllers": [
                {
                    "Name": "AMD Radeon 780M",
                    "AdapterRAM": 2147483648,
                    "PNPDeviceID": "PCI\\VEN_1002&DEV_15BF",
                    "DriverVersion": "32.0.12033.1030",
                },
                {
                    "Name": "NVIDIA GeForce RTX 4060 Laptop GPU",
                    "AdapterRAM": 8589934592,
                    "PNPDeviceID": "PCI\\VEN_10DE&DEV_28E0",
                    "DriverVersion": "32.0.15.6094",
                },
            ],
        }
    )
    calls = []

    def run(command, timeout_s=10.0):
        calls.append(command)
        return payload

    monkeypatch.setattr(system_probe, "_run_command", run)

    devices = system_probe.probe_system_devices("win32")

    assert [device.name for device in devices] == [
        "AMD Ryzen 7 7840U",
        "AMD Radeon 780M",
        "NVIDIA GeForce RTX 4060 Laptop GPU",
    ]
    assert devices[1].id.startswith("pnp:")
    assert devices[1].kind == "gpu"
    assert devices[1].memory_total == 2147483648
    assert devices[1].memory_available == 0
    assert devices[1].evidence["source"] == "cim_video_controller"
    assert devices[2].id.startswith("pnp:")
    assert devices[2].id != devices[1].id
    assert devices[2].evidence["driver_version"] == "32.0.15.6094"
    assert "Win32_VideoController" in calls[0][-1]


def test_windows_gpu_without_pnp_uses_reproducible_weak_evidence_hash():
    first = system_probe._windows_gpus(
        {
            "video_controllers": [
                {"Name": " Intel  Arc  GPU ", "AdapterRAM": 4096}
            ]
        }
    )[0]
    second = system_probe._windows_gpus(
        {
            "video_controllers": [
                {"Name": "intel arc gpu", "AdapterRAM": 4096}
            ]
        }
    )[0]

    normalized = "intel arc gpu|4096"
    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    assert first.id == f"windows-gpu-evidence:{expected}"
    assert second.id == first.id
    assert first.evidence["identity_persistent"] is False


def test_windows_gpu_with_pnp_marks_identity_persistent():
    gpu = system_probe._windows_gpus(
        {
            "video_controllers": [
                {
                    "Name": "NVIDIA GPU",
                    "PNPDeviceID": "PCI\\VEN_10DE&DEV_28E0",
                }
            ]
        }
    )[0]

    assert gpu.evidence["identity_persistent"] is True


def test_linux_nvidia_probe_parses_csv_without_splitting_quoted_names(monkeypatch):
    csv_payload = (
        '0, GPU-1234, "NVIDIA RTX, Ada", 8192, 6144, 560.35.03, 00000000:01:00.0\n'
        "1, GPU-5678, NVIDIA T4, N/A, N/A, 550.54.14, 00000000:02:00.0\n"
    )

    def run(command, timeout_s=10.0):
        return csv_payload if command[0] == "nvidia-smi" else ""

    monkeypatch.setattr(system_probe, "_run_command", run)
    monkeypatch.setattr(system_probe, "_glob_paths", lambda pattern: [])

    devices = system_probe.probe_system_devices("linux")
    gpus = [device for device in devices if device.kind == "gpu"]

    assert [device.id for device in gpus] == ["nvidia:GPU-1234", "nvidia:GPU-5678"]
    assert gpus[0].name == "NVIDIA RTX, Ada"
    assert gpus[0].memory_total == 8192 * 1024**2
    assert gpus[0].memory_available == 6144 * 1024**2
    assert gpus[0].evidence["pci_bus_id"] == "00000000:01:00.0"
    assert gpus[1].memory_total == 0


def test_linux_amd_probe_uses_drm_pci_evidence(monkeypatch):
    card = "/sys/class/drm/card1"
    values = {
        f"{card}/device/vendor": "0x1002\n",
        f"{card}/device/device": "0x73bf\n",
        f"{card}/device/uevent": "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:03:00.0\n",
        f"{card}/device/mem_info_vram_total": "8589934592\n",
        f"{card}/device/mem_info_vram_used": "2147483648\n",
        f"{card}/device/product_name": "AMD Radeon RX 6800\n",
    }
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: "")
    monkeypatch.setattr(system_probe, "_glob_paths", lambda pattern: [card])
    monkeypatch.setattr(system_probe, "_read_text", lambda path: values.get(str(path), ""))

    devices = system_probe.probe_system_devices("linux")
    gpu = next(device for device in devices if device.kind == "gpu")

    assert gpu.id == "pci:0000:03:00.0"
    assert gpu.name == "AMD Radeon RX 6800"
    assert gpu.memory_total == 8589934592
    assert gpu.memory_available == 6442450944
    assert gpu.evidence == {
        "source": "linux_drm_pci",
        "vendor": "amd",
        "vendor_id": "1002",
        "device_id": "73bf",
        "pci_bus_id": "0000:03:00.0",
        "driver": "amdgpu",
    }


def test_rocm_card_ordinals_reject_entire_batch_when_any_card_is_malformed():
    payload = json.dumps(
        {
            "card0": {"PCI Bus": "0000:03:00.0"},
            "card1": {"PCI Bus": 42},
        }
    )

    assert system_probe._parse_rocm_card_ordinals(payload) == {}


def test_rocm_card_ordinals_normalize_surrounding_spaces_and_hex_case():
    payload = json.dumps(
        {
            "card3": {"PCI Bus": "  000A:0B:0C.7  "},
        }
    )

    assert system_probe._parse_rocm_card_ordinals(payload) == {
        "000a:0b:0c.7": 3
    }


def test_rocm_card_ordinals_require_domain():
    payload = json.dumps(
        {
            "card3": {"PCI Bus": "0B:0C.7"},
        }
    )

    assert system_probe._parse_rocm_card_ordinals(payload) == {}


def test_linux_amd_probe_uses_base_rocm_ordinals_without_visibility(monkeypatch):
    cards = ["/sys/class/drm/card3", "/sys/class/drm/card7"]
    values = {
        f"{cards[0]}/device/vendor": "0x1002",
        f"{cards[0]}/device/device": "0x73bf",
        f"{cards[0]}/device/uevent": "PCI_SLOT_NAME=0000:03:00.0",
        f"{cards[1]}/device/vendor": "0x1002",
        f"{cards[1]}/device/device": "0x744c",
        f"{cards[1]}/device/uevent": "PCI_SLOT_NAME=0000:07:00.0",
    }
    payload = json.dumps(
        {
            "card0": {"PCI Bus": "0000:07:00.0"},
            "card1": {"PCI Bus": "0000:03:00.0"},
        }
    )
    commands = []

    def run(command, timeout_s=10.0):
        commands.append(command)
        return payload

    monkeypatch.setattr(system_probe, "_run_command", run)
    monkeypatch.setattr(system_probe, "_glob_paths", lambda pattern: cards)
    monkeypatch.setattr(system_probe, "_read_text", lambda path: values.get(str(path), ""))

    gpus = system_probe._linux_amd_gpus()

    assert [gpu.evidence["provider_ordinals"] for gpu in gpus] == [
        {"ROCMExecutionProvider": 1},
        {"ROCMExecutionProvider": 0},
    ]
    assert commands == [["rocm-smi", "--showuniqueid", "--showbus", "--json"]]
    assert system_probe._calibrate_rocm_ordinals(
        system_probe._parse_rocm_card_ordinals(payload),
        None,
        None,
    ) == {
        "0000:07:00.0": 0,
        "0000:03:00.0": 1,
    }


def test_calibrate_rocm_ordinals_reorders_by_bdf():
    base = {"0000:03:00.0": 0, "0000:07:00.0": 1}

    assert system_probe._calibrate_rocm_ordinals(
        base,
        "0000:07:00.0, 0000:03:00.0",
        None,
    ) == {
        "0000:07:00.0": 0,
        "0000:03:00.0": 1,
    }


def test_calibrate_rocm_ordinals_rejects_conflicts_and_numeric_tokens():
    base = {"0000:03:00.0": 0, "0000:07:00.0": 1}

    assert system_probe._calibrate_rocm_ordinals(
        base,
        "0000:03:00.0,0000:07:00.0",
        "0000:07:00.0,0000:03:00.0",
    ) == {}
    assert system_probe._calibrate_rocm_ordinals(base, "1,0", None) == {}
    assert system_probe._calibrate_rocm_ordinals(base, None, "0,1") == {}


def test_linux_nvidia_probe_calibrates_visible_devices_to_runtime_order(monkeypatch):
    payload = (
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0\n"
        "2, GPU-CCCC, NVIDIA C, 8192, 6144, 560.1, 00000000:03:00.0\n"
    )
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: payload)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "gpu-cccc, gpu-aaaa")
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)

    gpus = system_probe._linux_nvidia_gpus()

    assert [gpu.id for gpu in gpus] == [
        "nvidia:GPU-AAAA",
        "nvidia:GPU-BBBB",
        "nvidia:GPU-CCCC",
    ]
    assert gpus[0].evidence["provider_ordinals"] == {
        "CUDAExecutionProvider": 1,
        "TensorrtExecutionProvider": 1,
    }
    assert "provider_ordinals" not in gpus[1].evidence
    assert gpus[2].evidence["provider_ordinals"] == {
        "CUDAExecutionProvider": 0,
        "TensorrtExecutionProvider": 0,
    }


def test_linux_nvidia_probe_rejects_numeric_visibility_without_filtering_rows(monkeypatch):
    payload = (
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0"
    )
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: payload)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", " 0 ")

    gpus = system_probe._linux_nvidia_gpus()

    assert [gpu.id for gpu in gpus] == ["nvidia:GPU-AAAA", "nvidia:GPU-BBBB"]
    assert all("provider_ordinals" not in gpu.evidence for gpu in gpus)


def test_nvidia_runtime_ordinals_require_identical_complete_normalized_sequences(
    monkeypatch,
):
    rows = system_probe._nvidia_rows(
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0\n"
        "2, GPU-CCCC, NVIDIA C, 8192, 6144, 560.1, 00000000:03:00.0\n"
    )
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "GPU-CCCC, GPU-AAAA")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2, 0")

    assert system_probe._nvidia_runtime_ordinals(rows) == {}


def test_nvidia_runtime_ordinals_reject_prefix_of_complete_sequence(monkeypatch):
    rows = system_probe._nvidia_rows(
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0\n"
    )
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "GPU-AAAA, GPU-BBBB")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")

    assert system_probe._nvidia_runtime_ordinals(rows) == {}


def test_nvidia_runtime_ordinals_reject_reordered_complete_sequence(monkeypatch):
    rows = system_probe._nvidia_rows(
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0\n"
    )
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "GPU-AAAA, GPU-BBBB")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1, 0")

    assert system_probe._nvidia_runtime_ordinals(rows) == {}


def test_nvidia_runtime_ordinals_reject_invalid_sequence_members(monkeypatch):
    rows = system_probe._nvidia_rows(
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0\n"
    )
    invalid_pairs = (
        ("GPU-AAAA, GPU-AAAA", "0, 0"),
        ("GPU-AAAA, GPU-UNKNOWN", "0, 1"),
        ("GPU-AAAA,", "0, 1"),
    )

    for nvidia_value, cuda_value in invalid_pairs:
        monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", nvidia_value)
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", cuda_value)
        assert system_probe._nvidia_runtime_ordinals(rows) == {}


def test_linux_nvidia_probe_rejects_conflicting_visibility_without_ordinals(monkeypatch):
    payload = (
        "0, GPU-AAAA, NVIDIA A, 8192, 6144, 560.1, 00000000:01:00.0\n"
        "1, GPU-BBBB, NVIDIA B, 8192, 6144, 560.1, 00000000:02:00.0\n"
    )
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: payload)
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "GPU-AAAA")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")

    gpus = system_probe._linux_nvidia_gpus()

    assert [gpu.id for gpu in gpus] == ["nvidia:GPU-AAAA", "nvidia:GPU-BBBB"]
    assert all("provider_ordinals" not in gpu.evidence for gpu in gpus)


def test_linux_nvidia_probe_preserves_physical_ordinals_without_visibility_settings(monkeypatch):
    payload = "2, GPU-CCCC, NVIDIA C, 8192, 6144, 560.1, 00000000:03:00.0"
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: payload)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    gpu = system_probe._linux_nvidia_gpus()[0]

    assert gpu.evidence["provider_ordinals"] == {
        "CUDAExecutionProvider": 2,
        "TensorrtExecutionProvider": 2,
    }


def test_linux_drm_probe_ignores_non_amd_and_connector_entries(monkeypatch):
    paths = [
        "/sys/class/drm/card0",
        "/sys/class/drm/card0-HDMI-A-1",
        "/sys/class/drm/card1",
    ]
    values = {
        "/sys/class/drm/card0/device/vendor": "0x8086",
        "/sys/class/drm/card0-HDMI-A-1/device/vendor": "0x1002",
        "/sys/class/drm/card0-HDMI-A-1/device/device": "0xffff",
        "/sys/class/drm/card0-HDMI-A-1/device/uevent": (
            "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:ff:00.0\n"
        ),
        "/sys/class/drm/card1/device/vendor": "0x1002",
        "/sys/class/drm/card1/device/device": "0x164e",
        "/sys/class/drm/card1/device/uevent": "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:04:00.0\n",
    }
    monkeypatch.setattr(system_probe, "_run_command", lambda command, timeout_s=10.0: "")
    monkeypatch.setattr(system_probe, "_glob_paths", lambda pattern: paths)
    monkeypatch.setattr(system_probe, "_read_text", lambda path: values.get(str(path), ""))

    gpus = [
        device
        for device in system_probe.probe_system_devices("linux")
        if device.kind == "gpu"
    ]

    assert len(gpus) == 1
    assert gpus[0].name == "AMD GPU 1002:164e"
    assert gpus[0].memory_total == 0
    assert "Radeon" not in gpus[0].name


def test_vip_probe_never_invents_model_or_tops():
    device = parse_vip_probe('{"available":true,"driver":"6.4.3"}')

    assert device is not None
    assert device.name == "VIP NPU"
    assert "VIP9000" not in device.name
    assert "tops" not in device.evidence
    assert device.evidence == {"available": True, "driver": "6.4.3"}


def test_vip_probe_preserves_explicit_model_and_tops_only_as_evidence():
    device = parse_vip_probe(
        '{"available":true,"model":"GC7000","tops":1.5,"architecture":"vivante"}'
    )

    assert device is not None
    assert device.name == "GC7000"
    assert device.architecture == "vivante"
    assert device.evidence["tops"] == 1.5
    assert device.state is DeviceState.AVAILABLE
    assert device.backends[0].runtime is RuntimeKind.VIP


def test_vip_parser_rejects_invalid_or_unavailable_payloads():
    assert parse_vip_probe("") is None
    assert parse_vip_probe("not-json") is None
    assert parse_vip_probe("[]") is None
    assert parse_vip_probe('{"available":false,"model":"VIP9000"}') is None
    assert parse_vip_probe('{"available":"yes"}') is None
    assert parse_vip_probe('{"available":true,"tops":NaN}') is None
    assert parse_vip_probe('{"available":true,"tops":Infinity}') is None
    assert parse_vip_probe('{"available":true,"tops":-Infinity}') is None


def test_vip_backend_uses_exit_status_without_inventing_specs(tmp_path):
    runner = tmp_path / "vip-runner"
    runner.write_bytes(b"")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="probe: npu_ok\n")

    device = probe_vip_backend(str(runner), command_runner=run, platform="linux")

    assert device is not None
    assert device.name == "VIP NPU"
    assert device.evidence == {"available": True, "probe": "runner_exit_code"}
    assert calls[0][0] == ["sudo", "-n", str(runner), "--probe", "--quiet"]

    for stdout in ('{"available":false}', "not-json"):
        device = probe_vip_backend(
            str(runner),
            platform="linux",
            command_runner=lambda *args, _stdout=stdout, **kwargs: SimpleNamespace(
                returncode=0, stdout=_stdout, stderr=""
            ),
        )

        assert device is None


def test_vip_backend_returns_none_for_unsupported_or_failed_probe(tmp_path):
    runner = tmp_path / "vip-runner"
    runner.write_bytes(b"")

    assert probe_vip_backend(str(runner), platform="win32") is None
    assert probe_vip_backend(str(tmp_path / "missing"), platform="linux") is None
    assert probe_vip_backend(
        str(runner),
        platform="linux",
        command_runner=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    ) is None

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    assert probe_vip_backend(
        str(runner), platform="linux", command_runner=timeout, timeout_s=1
    ) is None
