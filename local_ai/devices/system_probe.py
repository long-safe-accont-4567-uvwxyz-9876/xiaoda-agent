from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import platform as platform_module
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from local_ai.contracts import ComputeDevice, DeviceState

_ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "x64": "x86_64",
}


def _read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_command(command: list[str], timeout_s: float = 10.0) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _glob_paths(pattern: str) -> list[str]:
    return sorted(glob.glob(pattern))


def _parse_meminfo(payload: str) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        fields = value.strip().split()
        if fields and fields[0].isdigit():
            values[key] = int(fields[0]) * 1024
    total = values.get("MemTotal", 0)
    available = min(values.get("MemAvailable", values.get("MemFree", 0)), total)
    return total, available


def _cpuinfo_value(payload: str, key: str) -> str:
    wanted = key.casefold()
    for line in payload.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().casefold() == wanted:
            return value.strip()
    return ""


def _machine_architecture() -> str:
    machine = platform_module.machine().strip()
    return _ARCHITECTURE_ALIASES.get(machine.casefold(), machine or "unknown")


def _linux_cpu() -> ComputeDevice:
    architecture = _machine_architecture()
    device_tree_model = _read_text("/sys/firmware/devicetree/base/model").rstrip("\x00\r\n ")
    cpuinfo = _read_text("/proc/cpuinfo")
    hardware = _cpuinfo_value(cpuinfo, "Hardware")
    processor = _cpuinfo_value(cpuinfo, "model name")
    name = device_tree_model or processor or hardware or "CPU"
    total, available = _parse_meminfo(_read_text("/proc/meminfo"))
    evidence = {}
    if device_tree_model:
        evidence["device_tree_model"] = device_tree_model
    if processor:
        evidence["cpuinfo_model_name"] = processor
    if hardware:
        evidence["cpuinfo_hardware"] = hardware
    return ComputeDevice(
        id="cpu:0",
        name=name,
        kind="cpu",
        architecture=architecture,
        state=DeviceState.AVAILABLE,
        memory_total=total,
        memory_available=available,
        system={"platform": "linux"},
        evidence=evidence,
    )


def _non_negative_int(value: Any) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _nvidia_memory(value: str) -> int:
    normalized = value.strip()
    if not normalized or normalized.casefold() == "n/a":
        return 0
    try:
        memory_mib = float(normalized)
    except ValueError:
        return 0
    if not memory_mib.is_integer() or memory_mib < 0:
        return 0
    return int(memory_mib) * 1024**2


def _nvidia_rows(payload: str) -> list[tuple[str, ...]]:
    try:
        return [
            tuple(value.strip() for value in row)
            for row in csv.reader(payload.splitlines(), skipinitialspace=True)
            if len(row) == 7 and row[2].strip()
        ]
    except csv.Error:
        return []


def _normalized_visible_nvidia_rows(
    value: str | None,
    rows: list[tuple[str, ...]],
) -> list[tuple[str, ...]] | None:
    if value is None:
        return list(rows)
    tokens = [item.strip() for item in value.split(",")]
    if not tokens or any(not item for item in tokens):
        return None
    by_uuid = {row[1].casefold(): row for row in rows if row[1].casefold() != "n/a"}
    selected = []
    seen = set()
    for token in tokens:
        normalized = token.casefold()
        if not normalized.startswith("gpu-"):
            return None
        row = by_uuid.get(normalized)
        if row is None or row[1] in seen:
            return None
        selected.append(row)
        seen.add(row[1])
    return selected


def _nvidia_runtime_ordinals(rows: list[tuple[str, ...]]) -> dict[str, int]:
    nvidia_value = os.environ.get("NVIDIA_VISIBLE_DEVICES")
    cuda_value = os.environ.get("CUDA_VISIBLE_DEVICES")
    nvidia_rows = _normalized_visible_nvidia_rows(nvidia_value, rows)
    cuda_rows = _normalized_visible_nvidia_rows(cuda_value, rows)
    if nvidia_rows is None or cuda_rows is None:
        return {}
    if nvidia_value is None and cuda_value is None:
        return {row[1]: int(row[0]) for row in rows if row[0].isdigit()}
    normalized_rows = cuda_rows if nvidia_value is None else nvidia_rows
    if nvidia_value is not None and cuda_value is not None:
        nvidia_sequence = tuple(row[1] for row in nvidia_rows)
        cuda_sequence = tuple(row[1] for row in cuda_rows)
        if nvidia_sequence != cuda_sequence:
            return {}
    return {row[1]: ordinal for ordinal, row in enumerate(normalized_rows)}


def _linux_nvidia_gpus() -> list[ComputeDevice]:
    payload = _run_command(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.free,driver_version,pci.bus_id",
            "--format=csv,noheader,nounits",
        ]
    )
    rows = _nvidia_rows(payload)
    runtime_ordinals = _nvidia_runtime_ordinals(rows)
    devices = []
    try:
        for row in rows:
            index, uuid, name, total_value, free_value, driver, pci_bus_id = (
                value for value in row
            )
            total = _nvidia_memory(total_value)
            available = min(_nvidia_memory(free_value), total)
            identity = uuid if uuid and uuid.casefold() != "n/a" else index
            evidence = {"source": "nvidia_smi_csv", "vendor": "nvidia"}
            if index.isdigit():
                evidence["index"] = index
            ordinal = runtime_ordinals.get(uuid)
            if ordinal is not None:
                evidence["provider_ordinals"] = {
                    "CUDAExecutionProvider": ordinal,
                    "TensorrtExecutionProvider": ordinal,
                }
            if uuid and uuid.casefold() != "n/a":
                evidence["uuid"] = uuid
            if driver and driver.casefold() != "n/a":
                evidence["driver_version"] = driver
            if pci_bus_id and pci_bus_id.casefold() != "n/a":
                evidence["pci_bus_id"] = pci_bus_id
            devices.append(
                ComputeDevice(
                    id=f"nvidia:{identity}",
                    name=name,
                    kind="gpu",
                    architecture=_machine_architecture(),
                    state=DeviceState.AVAILABLE,
                    memory_total=total,
                    memory_available=available,
                    system={"platform": "linux"},
                    evidence=evidence,
                )
            )
    except (csv.Error, ValueError):
        return []
    return devices


def _uevent_values(payload: str) -> dict[str, str]:
    values = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and value:
            values[key] = value.strip()
    return values


def _sysfs_hex(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    return normalized if re.fullmatch(r"[0-9a-f]{4}", normalized) else ""


def _parse_rocm_card_ordinals(payload: str) -> dict[str, int]:
    try:
        cards = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(cards, dict):
        return {}
    ordinals = {}
    for card_name, card in cards.items():
        match = re.fullmatch(r"card([0-9]+)", card_name)
        if match is None or not isinstance(card, dict):
            return {}
        pci_bus_id = card.get("PCI Bus")
        if not isinstance(pci_bus_id, str):
            return {}
        normalized = pci_bus_id.strip().casefold()
        if re.fullmatch(
            r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", normalized
        ) is None:
            return {}
        if normalized in ordinals:
            return {}
        ordinals[normalized] = int(match.group(1))
    return ordinals


def _calibrate_rocm_ordinals(
    base: dict[str, int],
    hip: str | None,
    rocr: str | None,
) -> dict[str, int]:
    if hip is None and rocr is None:
        return dict(base)

    normalized_base = {bdf.casefold(): ordinal for bdf, ordinal in base.items()}

    def normalize(value: str | None) -> list[str] | None:
        if value is None:
            return list(normalized_base)
        tokens = [token.strip().casefold() for token in value.split(",")]
        if not tokens or any(
            not token
            or re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", token)
            is None
            for token in tokens
        ):
            return None
        if len(tokens) != len(set(tokens)) or any(token not in normalized_base for token in tokens):
            return None
        return tokens

    hip_sequence = normalize(hip)
    rocr_sequence = normalize(rocr)
    if hip_sequence is None or rocr_sequence is None:
        return {}
    if hip is not None and rocr is not None and hip_sequence != rocr_sequence:
        return {}
    sequence = rocr_sequence if hip is None else hip_sequence
    return {bdf: ordinal for ordinal, bdf in enumerate(sequence)}


def _linux_amd_gpus() -> list[ComputeDevice]:
    base_ordinals = _parse_rocm_card_ordinals(
        _run_command(["rocm-smi", "--showuniqueid", "--showbus", "--json"])
    )
    runtime_ordinals = _calibrate_rocm_ordinals(
        base_ordinals,
        os.environ.get("HIP_VISIBLE_DEVICES"),
        os.environ.get("ROCR_VISIBLE_DEVICES"),
    )
    devices = []
    for card_value in _glob_paths("/sys/class/drm/card[0-9]*"):
        card = Path(card_value)
        if re.fullmatch(r"card[0-9]+", card.name) is None:
            continue
        vendor_id = _sysfs_hex(_read_text(card / "device/vendor"))
        if vendor_id != "1002":
            continue
        device_id = _sysfs_hex(_read_text(card / "device/device"))
        uevent = _uevent_values(_read_text(card / "device/uevent"))
        pci_bus_id = uevent.get("PCI_SLOT_NAME", "")
        if not device_id or not pci_bus_id:
            continue
        total = _non_negative_int(_read_text(card / "device/mem_info_vram_total"))
        used = min(
            _non_negative_int(_read_text(card / "device/mem_info_vram_used")),
            total,
        )
        product_name = _read_text(card / "device/product_name").strip()
        evidence = {
            "source": "linux_drm_pci",
            "vendor": "amd",
            "vendor_id": vendor_id,
            "device_id": device_id,
            "pci_bus_id": pci_bus_id,
        }
        driver = uevent.get("DRIVER", "")
        if driver:
            evidence["driver"] = driver
        ordinal = runtime_ordinals.get(pci_bus_id.casefold())
        if ordinal is not None:
            evidence["provider_ordinals"] = {"ROCMExecutionProvider": ordinal}
        devices.append(
            ComputeDevice(
                id=f"pci:{pci_bus_id}",
                name=product_name or f"AMD GPU {vendor_id}:{device_id}",
                kind="gpu",
                architecture=_machine_architecture(),
                state=DeviceState.AVAILABLE,
                memory_total=total,
                memory_available=total - used,
                system={"platform": "linux"},
                evidence=evidence,
            )
        )
    return devices


_WINDOWS_ARCHITECTURES = {
    0: "x86",
    5: "arm",
    6: "ia64",
    9: "x86_64",
    12: "aarch64",
}


def _windows_payload() -> dict[str, Any]:
    script = (
        "$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1;"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$gpu=@(Get-CimInstance Win32_VideoController | Select-Object "
        "Name,AdapterRAM,PNPDeviceID,DriverVersion);"
        "[pscustomobject]@{cpu=[pscustomobject]@{Name=$cpu.Name;"
        "Architecture=$cpu.Architecture;"
        "TotalPhysicalMemory=$os.TotalVisibleMemorySize*1KB;"
        "FreePhysicalMemory=$os.FreePhysicalMemory};"
        "video_controllers=$gpu} | ConvertTo-Json -Compress -Depth 4"
    )
    raw = _run_command(["powershell", "-NoProfile", "-Command", script])
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _windows_cpu_from_payload(payload: dict[str, Any]) -> ComputeDevice:
    cpu_payload = payload.get("cpu", payload)
    if not isinstance(cpu_payload, dict):
        cpu_payload = {}
    name = cpu_payload.get("Name") if isinstance(cpu_payload.get("Name"), str) else ""
    architecture_value = cpu_payload.get("Architecture")
    architecture = _WINDOWS_ARCHITECTURES.get(architecture_value, _machine_architecture())
    total = cpu_payload.get("TotalPhysicalMemory", 0)
    free_kib = cpu_payload.get("FreePhysicalMemory", 0)
    total = total if type(total) is int and total >= 0 else 0
    available = free_kib * 1024 if type(free_kib) is int and free_kib >= 0 else 0
    available = min(available, total)
    evidence = {}
    if name:
        evidence["windows_processor_name"] = name.strip()
    if type(architecture_value) is int:
        evidence["windows_processor_architecture"] = architecture_value
    return ComputeDevice(
        id="cpu:0",
        name=name.strip() or "CPU",
        kind="cpu",
        architecture=architecture,
        state=DeviceState.AVAILABLE,
        memory_total=total,
        memory_available=available,
        system={"platform": "windows"},
        evidence=evidence,
    )


def _windows_cpu() -> ComputeDevice:
    return _windows_cpu_from_payload(_windows_payload())


def _normalized_evidence_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _windows_gpu_id(
    pnp_device_id: str,
    index: int,
    *,
    name: str = "",
    adapter_ram: int = 0,
) -> tuple[str, str, bool]:
    normalized = pnp_device_id.strip().casefold()
    match = re.search(
        r"(?:^|\\)VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})(?:&|$)",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        weak_evidence = f"{_normalized_evidence_text(name)}|{adapter_ram}"
        digest = hashlib.sha256(weak_evidence.encode("utf-8")).hexdigest()[:16]
        return f"windows-gpu-evidence:{digest}", "unknown", False
    vendor_id, _ = (value.casefold() for value in match.groups())
    vendor = {"10de": "nvidia", "1002": "amd", "8086": "intel"}.get(
        vendor_id, "unknown"
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"pnp:{digest}", vendor, True


def _windows_gpus(payload: dict[str, Any]) -> list[ComputeDevice]:
    controllers = payload.get("video_controllers", ())
    if isinstance(controllers, dict):
        controllers = [controllers]
    if not isinstance(controllers, list):
        return []
    devices = []
    for index, controller in enumerate(controllers):
        if not isinstance(controller, dict):
            continue
        name = controller.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        pnp_device_id = controller.get("PNPDeviceID")
        if not isinstance(pnp_device_id, str):
            pnp_device_id = ""
        total = _non_negative_int(controller.get("AdapterRAM"))
        device_id, vendor, identity_persistent = _windows_gpu_id(
            pnp_device_id,
            index,
            name=name,
            adapter_ram=total,
        )
        evidence = {
            "source": "cim_video_controller",
            "vendor": vendor,
            "identity_persistent": identity_persistent,
        }
        if pnp_device_id:
            evidence["pnp_device_id"] = pnp_device_id
        driver = controller.get("DriverVersion")
        if isinstance(driver, str) and driver.strip():
            evidence["driver_version"] = driver.strip()
        devices.append(
            ComputeDevice(
                id=device_id,
                name=name.strip(),
                kind="gpu",
                architecture=_machine_architecture(),
                state=DeviceState.AVAILABLE,
                memory_total=total,
                memory_available=0,
                system={"platform": "windows"},
                evidence=evidence,
            )
        )
    return devices


def probe_system_devices(platform: str | None = None) -> list[ComputeDevice]:
    target = (platform or sys.platform).casefold()
    if target.startswith("linux"):
        return [_linux_cpu(), *_linux_nvidia_gpus(), *_linux_amd_gpus()]
    if target.startswith("win"):
        payload = _windows_payload()
        return [_windows_cpu_from_payload(payload), *_windows_gpus(payload)]
    return []
