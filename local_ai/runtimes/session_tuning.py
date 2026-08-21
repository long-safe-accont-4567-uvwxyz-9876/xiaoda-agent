"""ORT 会话自动调优 — 算力探测 + SessionOptions/ProviderOptions 动态构建。

对应方案"整体架构分层"的会话动态构建层：启动时探测一次硬件快照，
按平台 / CPU 架构 / GPU 厂商自动生成：

- ``SessionOptions``（线程数、DML 特判、CPU fallback 门控）
- ``provider_options``（TensorRT FP16 / 引擎缓存 / 显存上限、CUDA 启发搜索）

三个会话构建点统一接入本模块，避免双实现漂移：
- ``local_ai/runtimes/base.py``（Reranker 等）
- ``memory/local_embed.py``（Embedding 业务主路径）
- ``local_ai/devices/ort_providers.py``（探测 verify）
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# onnxruntime 为可选依赖：未安装时本模块只提供纯 Python 的探测/调优决策，
# 不构造真实 SessionOptions（build_session_options 返回 None）。
try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - 依赖缺失路径
    ort = None

# N 卡 TRT EP 自动开 FP16；设 ORT_TRT_FP16=0 关闭（个别算子 FP16 精度敏感）
_ENV_TRT_FP16 = "ORT_TRT_FP16"
# 纯 CPU 会话显式线程数；设 ORT_INTRA_OP_THREADS 覆盖物理核数
_ENV_CPU_THREADS = "ORT_INTRA_OP_THREADS"
# TRT 引擎缓存目录；设 LOCAL_AI_CACHE_DIR 覆盖默认 ~/.ai-agent/data/ort_cache
_ENV_CACHE_DIR = "LOCAL_AI_CACHE_DIR"

# 显式 EP 优先级：值越小越优先（recommend() 排序用）
# VIPLite（VIP9000 NPU）为本地专用推理设备，优先级最高，避免设备可用时
# 全部 embedding 推理落回 CPU（此前缺失导致 NPU 永远排在 CPU 之后）。
_EP_RANK = {
    "VIPLite": 0,
    "TensorrtExecutionProvider": 0,
    "CUDAExecutionProvider": 1,
    "ROCMExecutionProvider": 1,
    "DmlExecutionProvider": 1,
    "CPUExecutionProvider": 2,
}
_DEFAULT_RANK = 3


@dataclass(frozen=True)
class HardwareSnap:
    """启动时探测一次并缓存的算力信息。"""

    platform: str  # windows / linux / darwin / ...
    architecture: str  # x86_64 / aarch64 / ...
    cpu_logical: int
    cpu_physical: int | None
    gpu_vendor: str | None  # nvidia / amd / intel / ...
    gpu_name: str | None
    vram_mb: int


def _host_platform() -> str:
    import platform as _platform

    target = _platform.system().casefold()
    if target.startswith("win"):
        return "windows"
    if target.startswith("linux"):
        return "linux"
    if target.startswith("darwin"):
        return "darwin"
    return target or "unknown"


def _host_architecture() -> str:
    import platform as _platform

    aliases = {"amd64": "x86_64", "arm64": "aarch64"}
    machine = _platform.machine().strip().casefold()
    return aliases.get(machine, machine or "unknown")


def _cpu_physical() -> int | None:
    try:
        import psutil  # 可选依赖，失败回退 logical

        count = psutil.cpu_count(logical=False)
        if isinstance(count, int) and count > 0:
            return count
    except ImportError:
        logger.debug("session_tuning.psutil_not_available")
    except Exception as e:
        logger.debug("session_tuning.cpu_physical_failed error={}", str(e))
    return None


def _gpu_snapshot() -> tuple[str | None, str | None, int]:
    """从系统探测复用 GPU 信息；失败时静默返回 (None, None, 0)。

    probe_system_devices 启动探测开销不小（Windows 走 PowerShell CIM），
    但仅首次调用；失败不影响推理（无 GPU 时走 CPU 兜底）。
    """
    try:
        from local_ai.devices.system_probe import probe_system_devices

        devices = probe_system_devices()
    except Exception as error:  # noqa: BLE001
        logger.debug("session_tuning.gpu_probe_failed error={}", str(error))
        return None, None, 0
    best_vram = 0
    vendor = name = None
    for device in devices:
        if device.kind != "gpu" or device.state.value != "available":
            continue
        if device.memory_available > best_vram:
            best_vram = device.memory_available // (1024 * 1024)
            vendor = device.evidence.get("vendor") or device.system.get("vendor")
            name = device.name
    return vendor, name, best_vram


_hardware_lock = threading.Lock()
_hardware_cache: HardwareSnap | None = None


def detect_hardware(*, force: bool = False) -> HardwareSnap:
    """探测一次硬件并缓存（线程安全）。后续调用直接复用缓存。

    Args:
        force: 强制重新探测（测试用；生产无需）。
    """
    global _hardware_cache
    if _hardware_cache is not None and not force:
        return _hardware_cache
    with _hardware_lock:
        if _hardware_cache is not None and not force:
            return _hardware_cache
        logical = os.cpu_count() or 0
        physical = _cpu_physical()
        vendor, name, vram = _gpu_snapshot()
        snap = HardwareSnap(
            platform=_host_platform(),
            architecture=_host_architecture(),
            cpu_logical=logical,
            cpu_physical=physical,
            gpu_vendor=vendor,
            gpu_name=name,
            vram_mb=vram,
        )
        _hardware_cache = snap
        logger.debug(
            "session_tuning.hardware platform={} arch={} cpu={}/{} gpu={} vram_mb={}",
            snap.platform,
            snap.architecture,
            snap.cpu_logical,
            snap.cpu_physical,
            snap.gpu_vendor,
            snap.vram_mb,
        )
        return snap


def _reset_hardware_cache() -> None:
    """清除硬件快照缓存（测试隔离用）。"""
    global _hardware_cache
    with _hardware_lock:
        _hardware_cache = None


def default_engine_cache_dir() -> Path:
    """TRT 引擎缓存根目录。

    默认 ``KIOXIA_DATA_DIR``（或 ``~/.ai-agent/data``）下的 ``ort_cache``；
    env ``LOCAL_AI_CACHE_DIR`` 显式覆盖。仅返回路径，不强制创建。
    """
    override = os.getenv(_ENV_CACHE_DIR, "").strip()
    if override:
        return Path(override) / "ort_cache"
    base = os.getenv("KIOXIA_DATA_DIR", "").strip()
    if not base:
        base = str(Path.home() / ".ai-agent" / "data")
    return Path(base) / "ort_cache"


def provider_rank(provider: str) -> int:
    """显式 EP 优先级（值越小越优先）：VIPLite(NPU)/TRT 优先，其次 CUDA/ROCM/DML，CPU 兜底。"""
    return _EP_RANK.get(provider, _DEFAULT_RANK)


def build_session_options(
    providers: list[str],
    *,
    hardware: HardwareSnap | None = None,
    intra_op_threads: int | None = None,
    inter_op_threads: int | None = None,
    disable_cpu_fallback: bool = True,
    ort_module: Any | None = None,
) -> Any | None:
    """按 provider 自动构建 ``onnxruntime.SessionOptions``。

    - 纯 CPU（香橙派 ARM 全核）：显式 ``intra_op_num_threads = 物理核心数``，
      使 ORT 用满 CPU 全部性能；env ``ORT_INTRA_OP_THREADS`` 可覆盖。
    - DML：保留 ``enable_mem_pattern=False`` + ``ORT_SEQUENTIAL``（兼容旧行为）。
    - 非 CPU：保留 ``session.disable_cpu_ep_fallback=1``（禁静默回退 CPU）。

    Args:
        providers: 目标 provider 列表（通常为单元素，如 ["CPUExecutionProvider"]）。
        hardware: 硬件快照；None 时内部探测。
        intra_op_threads: 显式 intra-op 线程数；None 时纯 CPU 用物理核数。
        inter_op_threads: 显式 inter-op 线程数；None 时保持 ORT 默认。
        disable_cpu_fallback: 非 CPU 时是否禁用 CPU EP 回退。
        ort_module: onnxruntime 模块；默认用本模块导入的 ort（测试可注入 fake）。

    Returns:
        构造的 ``SessionOptions``；onnxruntime 未安装时返回 None。
    """
    _ort = ort_module if ort_module is not None else ort
    if _ort is None:
        return None
    snap = hardware or detect_hardware()
    opts = _ort.SessionOptions()

    is_pure_cpu = providers == ["CPUExecutionProvider"]
    if is_pure_cpu:
        threads = intra_op_threads
        if threads is None:
            env_threads = os.getenv(_ENV_CPU_THREADS, "").strip()
            if env_threads.isdigit() and int(env_threads) > 0:
                threads = int(env_threads)
            else:
                threads = snap.cpu_physical or snap.cpu_logical or 0
        if threads and threads > 0:
            opts.intra_op_num_threads = threads
        if inter_op_threads is not None and inter_op_threads > 0:
            opts.inter_op_num_threads = inter_op_threads

    if "DmlExecutionProvider" in providers:
        opts.enable_mem_pattern = False
        opts.execution_mode = _ort.ORT_SEQUENTIAL

    if disable_cpu_fallback and not is_pure_cpu:
        opts.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    return opts


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    try:
        return float(value)
    except ValueError:
        return default


def auto_provider_options(
    provider: str,
    *,
    hardware: HardwareSnap | None = None,
    engine_cache_dir: Path | None = None,
    fp16: bool | None = None,
    max_workspace_mb: int | None = None,
) -> dict[str, Any] | None:
    """按 provider 自动生成 provider_options；无需要时返回 None。

    - ``TensorrtExecutionProvider``：N 卡默认开 FP16（``ORT_TRT_FP16=0`` 关）、
      ``trt_max_workspace_size``（默认显存的 40%）、引擎缓存
      （``trt_engine_cache_enable`` + ``trt_engine_cache_path``）、
      ``trt_engine_hw_compatible=1``（不同 CPU 间缓存可复用）。
    - ``CUDAExecutionProvider``：``cudnn_conv_algo_search=HEURISTIC``
      （比 ORT 默认 EXHAUSTIVE 快，首次建图更快）。

    Args:
        provider: EP 名称。
        hardware: 硬件快照；None 时内部探测。
        engine_cache_dir: TRT 引擎缓存目录；None 时用默认缓存路径。
        fp16: TRT FP16 开关；None 时按 N 卡自动（有 N 卡开）。
        max_workspace_mb: TRT 最大 workspace（MB）；None 时按显存 40%。

    Returns:
        provider_options dict；该 provider 无需注入参数时返回 None。
    """
    if provider == "TensorrtExecutionProvider":
        snap = hardware or detect_hardware()
        is_nvidia = snap.gpu_vendor == "nvidia"
        fp16_value = fp16 if fp16 is not None else (
            os.getenv(_ENV_TRT_FP16, "1") != "0" and is_nvidia
        )
        opts: dict[str, Any] = {"trt_fp16_enable": "1" if fp16_value else "0"}
        if max_workspace_mb is None:
            max_workspace_mb = max(int(snap.vram_mb * 0.4), 0)
        if max_workspace_mb and max_workspace_mb > 0:
            opts["trt_max_workspace_size"] = str(max_workspace_mb * 1024 * 1024)
        cache_dir = engine_cache_dir
        if cache_dir is None:
            cache_dir = default_engine_cache_dir()
        if cache_dir:
            opts["trt_engine_cache_enable"] = "1"
            opts["trt_engine_cache_path"] = str(cache_dir)
            opts["trt_engine_hw_compatible"] = "1"
        return opts
    if provider == "CUDAExecutionProvider":
        return {"cudnn_conv_algo_search": "HEURISTIC"}
    return None


__all__ = [
    "HardwareSnap",
    "detect_hardware",
    "default_engine_cache_dir",
    "provider_rank",
    "build_session_options",
    "auto_provider_options",
    "_reset_hardware_cache",
]