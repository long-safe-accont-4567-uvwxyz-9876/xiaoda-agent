"""跨平台算力设备实时状态采集：CPU 利用率/内存、GPU 利用率/显存、NPU 状态。

供 WebUI「算力设备」页轮询观测负载。所有采集失败静默返回空字段，
绝不抛异常（设备状态采集是展示性功能，不应影响主流程）。
"""
from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import time
from typing import Any

from loguru import logger

from local_ai.contracts import ComputeDevice

# CPU 占用采样（/proc/stat 差值法，跨调用计算）
_CPU_SAMPLE: dict[str, Any] = {"ts": 0.0, "total": 0, "idle": 0}


def _run_command(command: list[str], timeout_s: float = 5.0) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # 与 system_probe._run_command 一致：显式 UTF-8 + errors=replace，
            # 避免 Windows PowerShell 按系统 OEM/ANSI 编码输出时解码崩溃。
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def cpu_stats() -> dict:
    """CPU 实时状态：核数 / 频率 / 占用百分比 / 内存占用。

    Linux 用 /proc 差值法；Windows/macOS 用 psutil 跨平台采样。
    """
    stats: dict[str, Any] = {
        "cores": os.cpu_count() or 0,
        "freq_mhz": None,
        "usage_pct": None,
        "memory_total": None,
        "memory_available": None,
    }
    if platform.system() != "Linux":
        try:
            import psutil as _ps
        except ImportError:  # noqa: BLE001
            return stats
        try:
            freq = _ps.cpu_freq()
            if freq is not None and freq.current:
                stats["freq_mhz"] = round(freq.current)
        except Exception:  # noqa: BLE001
            logger.warning("device_stats.cpu_freq_failed", exc_info=True)
        try:
            stats["usage_pct"] = round(_ps.cpu_percent(interval=None), 1)
        except Exception:  # noqa: BLE001
            logger.warning("device_stats.cpu_percent_failed", exc_info=True)
        try:
            vm = _ps.virtual_memory()
            stats["memory_total"] = vm.total
            stats["memory_available"] = vm.available
        except Exception:  # noqa: BLE001
            logger.warning("device_stats.virtual_memory_failed", exc_info=True)
        return stats
    # Linux：/proc/meminfo + /proc/cpuinfo + /proc/stat 差值法
    try:
        meminfo: dict[str, int] = {}
        for line in _read_text("/proc/meminfo").splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            fields = value.strip().split()
            if fields and fields[0].isdigit():
                meminfo[key] = int(fields[0]) * 1024
        total = meminfo.get("MemTotal", 0)
        available = min(meminfo.get("MemAvailable", meminfo.get("MemFree", 0)), total)
        if total:
            stats["memory_total"] = total
            stats["memory_available"] = available
    except Exception:  # noqa: BLE001
        logger.warning("device_stats.meminfo_parse_failed", exc_info=True)
    for line in _read_text("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            try:
                stats["freq_mhz"] = round(float(line.split(":", 1)[1].strip()))
            except ValueError:  # noqa: BLE001
                logger.debug("device_stats.cpu_mhz_parse_failed", exc_info=True)
            break
    try:
        parts = _read_text("/proc/stat").splitlines()[0].split()
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        now = time.monotonic()
        prev = _CPU_SAMPLE
        if prev["total"] and (now - prev["ts"]) > 0.2:
            dt = total - prev["total"]
            di = idle - prev["idle"]
            if dt > 0:
                stats["usage_pct"] = round((1 - di / dt) * 100, 1)
        _CPU_SAMPLE.update(ts=now, total=total, idle=idle)
    except Exception:  # noqa: BLE001
        logger.warning("device_stats.proc_stat_parse_failed", exc_info=True)
    return stats


def _nvidia_rows(payload: str) -> list[tuple[str, ...]]:
    try:
        return [
            tuple(value.strip() for value in row)
            for row in csv.reader(payload.splitlines(), skipinitialspace=True)
            if len(row) >= 2 and row[0].strip()
        ]
    except csv.Error:
        return []


def _gpu_stats_linux_nvidia(device: ComputeDevice) -> dict:
    index = str(device.evidence.get("index", ""))
    payload = _run_command(
        [
            "nvidia-smi",
            "--query-gpu=index,utilization.gpu,memory.total,memory.used,memory.free,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    for row in _nvidia_rows(payload):
        if row[0] != index:
            continue
        usage = row[1]
        try:
            return {
                "utilization_pct": round(float(usage), 1),
                "memory_total": int(row[2]) * 1024**2,
                "memory_used": int(row[3]) * 1024**2,
                "memory_available": int(row[4]) * 1024**2,
                "temperature_c": int(row[5]),
                "source": "nvidia_smi",
            }
        except ValueError:  # noqa: BLE001
            return {"source": "nvidia_smi"}
    return {}


def _gpu_stats_linux_amd(device: ComputeDevice) -> dict:
    payload = _run_command(
        ["rocm-smi", "--showuse", "--showmemuse", "--showtemp", "--json"]
    )
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    pci_bus_id = str(device.evidence.get("pci_bus_id", "")).casefold()
    for card_name, card in data.items():
        if not isinstance(card, dict):
            continue
        if str(card.get("PCI Bus", "")).casefold() != pci_bus_id:
            continue
        stats: dict[str, Any] = {"source": "rocm_smi"}
        use_value = card.get("GPU use (%)")
        if isinstance(use_value, str) and use_value.strip().isdigit():
            stats["utilization_pct"] = float(use_value.strip())
        mem_used = card.get("VRAM Total Used Memory (B)")
        mem_total = card.get("VRAM Total Memory (B)")
        if isinstance(mem_used, str) and mem_used.strip().isdigit():
            stats["memory_used"] = int(mem_used.strip())
        if isinstance(mem_total, str) and mem_total.strip().isdigit():
            stats["memory_total"] = int(mem_total.strip())
        return stats
    return {}


def _gpu_stats_windows(device: ComputeDevice) -> dict:
    # Windows：优先 nvidia-smi（若在 PATH），否则用性能计数器聚合
    vendor = device.evidence.get("vendor")
    if vendor == "nvidia":
        index = str(device.evidence.get("index", ""))
        payload = _run_command(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.total,memory.used,memory.free,temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        for row in _nvidia_rows(payload):
            if row[0] != index:
                continue
            try:
                return {
                    "utilization_pct": round(float(row[1]), 1),
                    "memory_total": int(row[2]) * 1024**2,
                    "memory_used": int(row[3]) * 1024**2,
                    "memory_available": int(row[4]) * 1024**2,
                    "temperature_c": int(row[5]),
                    "source": "nvidia_smi",
                }
            except ValueError:  # noqa: BLE001
                return {"source": "nvidia_smi"}
    # 通用：Get-Counter 3D 引擎聚合（需要两次采样取差值，较慢，限制单次）
    script = (
        # 强制 UTF-8 输出，避免 GBK 区域 PowerShell 输出解码失败
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$s=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' "
        "-ErrorAction SilentlyContinue).CounterSamples | "
        "Measure-Object -Property CookedValue -Sum;"
        "[pscustomobject]@{pct=[math]::Round($s.Sum,1)} | ConvertTo-Json -Compress"
    )
    raw = _run_command(["powershell", "-NoProfile", "-Command", script], timeout_s=8.0)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("pct"), (int, float)):
        return {"utilization_pct": round(float(data["pct"]), 1), "source": "perf_counter"}
    return {}


def gpu_stats(device: ComputeDevice) -> dict:
    """单个 GPU 设备实时状态（利用率 / 显存 / 温度）。"""
    if platform.system() == "Windows":
        return _gpu_stats_windows(device)
    vendor = device.evidence.get("vendor")
    if vendor == "nvidia":
        return _gpu_stats_linux_nvidia(device)
    if vendor == "amd":
        return _gpu_stats_linux_amd(device)
    return {}


def _devfreq_npu() -> dict:
    """Rockchip/常见 NPU 的 devfreq 节点：读取当前/最高频率（Hz）。

    devfreq 无标准 load 文件时占用率不可得，但频率可反映是否被驱动
    拉高。找不到节点时静默返回空字段。
    """
    stats: dict[str, Any] = {}
    if platform.system() != "Linux":
        return stats
    try:
        import glob as _glob

        node = _glob.glob("/sys/class/devfreq/*.npu/cur_freq")
        if not node:
            return stats
        cur = int(_read_text(node[0]).strip() or "0")
        if cur > 0:
            stats["freq_hz"] = cur
        max_node = "/".join(node[0].split("/")[:-1] + ["max_freq"])
        mx = int(_read_text(max_node).strip() or "0")
        if mx > 0:
            stats["max_freq_hz"] = mx
    except Exception:  # noqa: BLE001
        return stats
    return stats


def npu_stats() -> dict:
    """NPU 通用实时状态。

    Linux VIP NPU 无标准占用接口（无 rknpu debugfs / devfreq load），
    占用率不可得；补充 devfreq 频率供观测，其余字段保持空骨架。
    """
    stats: dict[str, Any] = {"utilization_pct": None, "memory_used": None}
    stats.update(_devfreq_npu())
    return stats


def attach_device_stats(devices: list[ComputeDevice]) -> dict[str, dict]:
    """为每个设备采集实时状态，返回 {device_id: stats}。"""
    result: dict[str, dict] = {}
    for device in devices:
        if device.kind == "cpu":
            result[device.id] = cpu_stats()
        elif device.kind == "gpu":
            result[device.id] = gpu_stats(device)
        elif device.kind == "npu":
            result[device.id] = npu_stats()
        else:
            result[device.id] = {}
    return result


__all__ = [
    "cpu_stats",
    "gpu_stats",
    "npu_stats",
    "attach_device_stats",
]
