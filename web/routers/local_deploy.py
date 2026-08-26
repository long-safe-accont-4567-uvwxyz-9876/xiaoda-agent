"""本地部署路由：向量嵌入引擎管理（API / 本地模型切换、启动/停止、日志）。

WebUI 侧边栏"本地部署"页：选择向量嵌入引擎——远程 API（硅基流动）
或内置本地 BGE 模型（NPU/CPU）。使用本地模型前必须先"启动"，
页面下方展示启动/运行日志。
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.config_service import get_config_service
from web.prompt_golden_cases import golden_cases_for_node
from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["local-deploy"], dependencies=[Depends(get_current_user)])

_NODE_UPDATE_LOCK = asyncio.Lock()

# prompt AB 跑分防重复触发：per-prompt 锁，同一 profile 同时只允许一轮跑分
_AB_RUN_LOCKS: dict[str, asyncio.Lock] = {}

# 设备探测缓存：5 分钟有效，避免每次刷新页面都 spawn runner 探测 NPU
_DEVICE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_DEVICE_CACHE_TTL = 300.0

# CPU 占用采样（/proc/stat 差值法，跨调用计算）
_CPU_SAMPLE: dict[str, Any] = {"ts": 0.0, "total": 0, "idle": 0}


def _persist_embed_mode_by_rule(vs: Any) -> str:
    """按持久化规则重算并写入 local_deploy.mode。

    规则（2026-08-13 用户确认）：仅当「本地引擎已启动」且「功能节点里有节点
    选择了本地模型」时持久化为 local（每次服务重启自动常驻）；二者缺一则
    持久化失效，写入 remote（下次重启默认走 API）。
    返回最终写入的 mode。
    """
    engine_running = False
    if vs is not None:
        try:
            engine_running = bool(
                vs.embed_engine_status().get("engine_running", False)
            )
        except (OSError, RuntimeError, ValueError, AttributeError):  # noqa: BLE001
            engine_running = False
    node_local = False
    try:
        from web.local_deploy_nodes import NODES, get_backend

        cfg = get_config_service()
        for node in NODES:
            if get_backend(cfg, node["id"]) == "local":
                node_local = True
                break
    except (ImportError, OSError, ValueError, RuntimeError):  # noqa: BLE001
        node_local = False
    mode = "local" if (engine_running and node_local) else "remote"
    try:
        get_config_service().set("local_deploy.mode", mode)
    except (OSError, ValueError, RuntimeError) as e:  # noqa: BLE001
        logger.warning("local_deploy.mode_persist_failed error={}", str(e))
    except Exception:
        logger.exception("local_deploy._persist_embed_mode_by_rule.unexpected_error")
    logger.info(
        "local_deploy.mode_persisted mode={} engine_running={} node_local={}",
        mode, engine_running, node_local,
    )
    return mode


def _cpu_stats_psutil(stats: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil as _ps
    except ImportError:
        return stats
    try:
        freq = _ps.cpu_freq()
        if freq is not None and freq.current:
            stats["freq_mhz"] = round(freq.current)
    except (OSError, RuntimeError, ValueError):
        logger.warning("local_deploy.cpu_freq_failed", exc_info=True)
    try:
        stats["usage_pct"] = round(_ps.cpu_percent(interval=None), 1)
    except (OSError, RuntimeError, ValueError):
        logger.warning("local_deploy.cpu_percent_failed", exc_info=True)
    return stats


def _cpu_stats_linux(stats: dict[str, Any]) -> dict[str, Any]:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("cpu MHz"):
                    stats["freq_mhz"] = round(float(line.split(":")[1].strip()))
                    break
    except (OSError, ValueError):
        logger.debug("local_deploy.cpuinfo_read_failed", exc_info=True)
    if not stats["freq_mhz"]:
        try:
            for i in range(64):
                for f in ("cpuinfo_max_freq", "cpuinfo_cur_freq"):
                    p = f"/sys/devices/system/cpu/cpu{i}/cpufreq/{f}"
                    if os.path.exists(p):
                        with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                            stats["freq_mhz"] = round(int(fh.read().strip()) / 1000)
                        break
                if stats["freq_mhz"]:
                    break
        except (OSError, ValueError):
            logger.debug("local_deploy.cpufreq_sysfs_read_failed", exc_info=True)
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            parts = f.readline().split()
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
    except (OSError, ValueError):
        logger.debug("local_deploy.proc_stat_read_failed", exc_info=True)
    return stats


def _cpu_stats() -> dict:
    stats: dict[str, Any] = {"cores": None, "freq_mhz": None, "usage_pct": None}
    try:
        stats["cores"] = os.cpu_count() or 0
    except (OSError, RuntimeError):
        logger.warning("local_deploy.cpu_count_failed", exc_info=True)
    if platform.system() != "Linux":
        return _cpu_stats_psutil(stats)
    return _cpu_stats_linux(stats)


def _npu_stats(vs: Any) -> dict:
    """NPU 实时状态（常驻流 / 占用 / 最近推理耗时），从向量库 provider 读取。"""
    d = {"resident": False, "busy": False, "last_call_ms": None, "calls": 0}
    try:
        prov = getattr(vs, "_local_provider", None)
        if prov is not None and hasattr(prov, "npu_stats"):
            d.update(prov.npu_stats())
    except (OSError, RuntimeError, ValueError, AttributeError) as e:  # noqa: BLE001
        logger.warning("local_deploy.npu_stats_failed error={}", str(e))
    except Exception:
        logger.exception("local_deploy._npu_stats.unexpected_error")
    return d


def _detect_cpu_model() -> str:
    """读取 CPU 型号：Linux 依次尝试 model name / Hardware / 设备树 / Processor；
    Windows 用 PowerShell CIM 拿型号（如 "12th Gen Intel(R) Core(TM) i7-12700H"）。"""
    if platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            for key in ("model name", "Hardware", "Processor"):
                m = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", txt, re.M)
                if m:
                    val = m.group(1).strip()
                    if val and val.lower() != "unknown":
                        return val
            # aarch64 无 model name 时读设备树型号（如 "Orange Pi 4 Pro"）
            dt_model = "/proc/device-tree/model"
            if os.path.exists(dt_model):
                try:
                    with open(dt_model, "r", encoding="utf-8", errors="ignore") as f:
                        val = f.read().rstrip("\x00").strip()
                    if val:
                        return val
                except OSError:
                    logger.debug("local_deploy.device_tree_model_read_failed", exc_info=True)
        except OSError:
            logger.debug("local_deploy.cpu_model_proc_read_failed", exc_info=True)
    if platform.system() == "Windows":
        # 零开销兜底：PROCESSOR_IDENTIFIER（"Intel64 Family 6 ..."）
        pid = os.environ.get("PROCESSOR_IDENTIFIER", "")
        try:
            import subprocess
            ps = ("powershell -NoProfile -Command "
                  '"Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name"')
            out = subprocess.run(ps, capture_output=True, text=True, timeout=15, check=False).stdout
            name = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
            if name:
                return name
        except (OSError, subprocess.SubprocessError):  # noqa: BLE001
            logger.debug("local_deploy.windows_cpu_model_query_failed", exc_info=True)
        return pid or platform.processor() or "CPU"
    return platform.processor() or "CPU"


def _detect_gpu_model() -> str:
    """检测 GPU 型号：Linux 用 lspci 查 VGA/3D 控制器；Windows 用 PowerShell CIM
    枚举显卡控制器（不依赖 nvidia-smi——它默认不在 PATH）。"""
    if platform.system() == "Linux":
        import subprocess
        try:
            out = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            for line in out.splitlines():
                low = line.lower()
                if "vga" in low or "3d controller" in low or "display controller" in low:
                    # 提取设备名：lspci 行形如 "01:00.0 VGA compatible controller: NVIDIA ..."
                    seg = line.split(":", 2)[-1].strip()
                    seg = re.sub(r"^VGA compatible controller:\s*", "", seg)
                    seg = re.sub(r"^3D controller:\s*", "", seg)
                    return seg.strip() or "GPU"
            return ""
        except (OSError, subprocess.SubprocessError):
            return ""
    if platform.system() == "Windows":
        return _detect_gpu_model_windows()
    return ""


def _detect_gpu_model_windows() -> str:
    """Windows：Get-CimInstance Win32_VideoController 枚举显卡（含核显+独显）。

    优先独立显卡（NVIDIA / AMD Radeon / GeForce），其次 Intel 核显，最后任意。
    """
    import subprocess
    try:
        ps = ("powershell -NoProfile -Command "
              '"Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"')
        out = subprocess.run(ps, capture_output=True, text=True, timeout=15, check=False).stdout
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not names:
            return ""
        prefer = [n for n in names
                  if any(k in n.lower() for k in ("nvidia", "radeon", "amd", "geforce", "rtx", "gtx"))]
        return (prefer or names)[0]
    except (OSError, subprocess.SubprocessError):  # noqa: BLE001
        return ""


def _probe_npu_available() -> bool:
    try:
        from memory.npu_embed import probe_npu
        return probe_npu()
    except (ImportError, OSError, RuntimeError, ValueError) as e:
        logger.warning("local_deploy.device_npu_probe_failed error={}", str(e))
        return False
    except Exception:
        logger.exception("local_deploy._detect_devices.unexpected_error")
        return False


def _build_device_list(npu_ok: bool, cpu_model: str, gpu_model: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "cpu",
            "name": "CPU",
            "model": cpu_model or "CPU",
            "desc": "onnxruntime 推理，离线可用（默认）",
            "available": True,
        },
        {
            "id": "npu",
            "name": "NPU",
            "model": "NPU" if npu_ok else "未检测到可用 NPU",
            "desc": "NPU 常驻流加速，短文本 CPU / 长文本 NPU 自适应" if npu_ok
                    else "未检测到可用 NPU（需 Linux 驱动与 sudo 免密）",
            "available": npu_ok,
        },
        {
            "id": "gpu",
            "name": "GPU",
            "model": gpu_model or "未检测到独立显卡",
            "desc": "此模型暂不支持 GPU 推理",
            "available": False,
        },
    ]


def _detect_devices() -> dict:
    now = time.monotonic()
    if _DEVICE_CACHE["data"] is not None and (now - _DEVICE_CACHE["ts"]) < _DEVICE_CACHE_TTL:
        return _DEVICE_CACHE["data"]
    npu_ok = _probe_npu_available()
    cpu_model = _detect_cpu_model()
    gpu_model = _detect_gpu_model()
    current = ""
    try:
        current = str(get_config_service().get("local_deploy.device", "") or "")
    except (OSError, ValueError, RuntimeError):
        current = ""
    devices = _build_device_list(npu_ok, cpu_model, gpu_model)
    data = {"current": current, "devices": devices}
    _DEVICE_CACHE.update(ts=now, data=data)
    return data


def _get_vector_store(request: Request) -> Any:
    """从全局 core 获取 VectorStore 单例（可能为 None：向量库未初始化）。"""
    core = getattr(request.app.state, "core", None)
    return getattr(core, "_vec_store", None) if core is not None else None


def _fallback_status() -> dict:
    """向量库不可用时返回的环境状态（只读，页面仍可展示）。"""
    return {
        "mode": os.getenv("EMBED_MODE", "local"),
        "engine_running": False,
        "backend": os.getenv("LOCAL_EMBED_BACKEND", "auto"),
        "api_configured": bool(
            os.getenv("SILICONFLOW_API_KEY") or os.getenv("EMBED_API_KEY")),
        "model_dir": "",
        "dimensions": 0,
        "available": False,
    }


def _authoritative_devices_to_list(
    authoritative: list[Any],
    current: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    devices: list[dict[str, Any]] = []
    kinds: set[str] = set()
    for device in authoritative:
        item = device.to_dict()
        kinds.add(str(item["kind"]))
        devices.append({
            "id": item["id"],
            "name": item["kind"].upper(),
            "model": item["name"],
            "desc": ", ".join(
                backend["provider"] for backend in item.get("backends", [])
            ) or item["architecture"],
            "available": item["state"] == "available",
            "active": item["id"] == current,
            "stats": {
                "memory_total": item["memory_total"],
                "memory_available": item["memory_available"],
            },
        })
    return devices, kinds


def _ensure_ui_slots(devices: list[dict[str, Any]], kinds: set[str], current: str) -> None:
    if "npu" not in kinds:
        devices.append({
            "id": "npu",
            "name": "NPU",
            "model": "未检测到可用 NPU",
            "desc": "未检测到可用 NPU（需 Linux 驱动与 sudo 免密）",
            "available": False,
            "active": current == "npu",
            "stats": {"status": "unavailable"},
        })
    if "gpu" not in kinds:
        devices.append({
            "id": "gpu",
            "name": "GPU",
            "model": "未检测到独立显卡",
            "desc": "此模型暂不支持 GPU 推理",
            "available": False,
            "active": current == "gpu",
            "stats": {"status": "unavailable"},
        })


async def _attach_live_stats(data: dict, request: Request) -> None:
    vs = _get_vector_store(request)
    for dev in data.get("devices", []):
        if dev["id"] == "cpu":
            dev["stats"] = await asyncio.to_thread(_cpu_stats)
        elif dev["id"] == "npu":
            # 与 CPU 分支一致下放线程池：NPU 探测含同步子进程调用
            dev["stats"] = await asyncio.to_thread(_npu_stats, vs)
        else:
            dev["stats"] = {"status": "unavailable"}


@router.get("/local-deploy/devices", response_model=Envelope[dict])
async def local_deploy_devices(request: Request) -> Any:
    services = getattr(request.app.state, "local_ai", None)
    if services is not None:
        authoritative = await asyncio.to_thread(services.devices.scan)
        configured = str(get_config_service().get("local_deploy.device", "") or "")
        current = next(
            (device.id for device in authoritative if device.id == configured),
            next((device.id for device in authoritative if device.kind == configured), ""),
        )
        devices, kinds = _authoritative_devices_to_list(authoritative, current)
        _ensure_ui_slots(devices, kinds, current)
        return Envelope(data={
            "current": current,
            "devices": devices,
            "runtime_backend": os.getenv("LOCAL_EMBED_BACKEND", "auto"),
        })
    data = await asyncio.to_thread(_detect_devices)
    data["runtime_backend"] = os.getenv("LOCAL_EMBED_BACKEND", "auto")
    await _attach_live_stats(data, request)
    return Envelope(data=data)


@router.post("/local-deploy/device", response_model=Envelope[dict])
async def local_deploy_set_device(request: Request, body: dict) -> Any:
    """持久化算力设备选择（cpu / npu）。切换后需重启服务生效。

    校验：npu 必须实测可用（probe_npu），gpu 恒不可选（模型不支持）。
    """
    device = str((body or {}).get("device", "")).strip().lower()
    if device not in ("cpu", "npu"):
        raise HTTPException(status_code=422, detail="device must be 'cpu' or 'npu'")
    if device == "npu":
        from memory.npu_embed import probe_npu
        # sudo -n 探测最长 15s，必须在线程池执行避免阻塞事件循环
        if not await asyncio.to_thread(probe_npu):
            raise HTTPException(status_code=409, detail="未检测到可用 NPU 设备")
    get_config_service().set("local_deploy.device", device)
    # 清空探测缓存，下次查询立即反映新选择
    _DEVICE_CACHE["data"] = None
    logger.info("local_deploy.device_set device={} (restart required)", device)
    return Envelope(data={"device": device, "need_restart": True})


@router.get("/local-deploy/status", response_model=Envelope[dict])
async def local_deploy_status(request: Request) -> Any:
    """当前 embedding 引擎状态：模式 / 本地引擎运行状态 / API 配置。"""
    vs = _get_vector_store(request)
    if vs is None:
        logger.info("local_deploy.status vector_store_unavailable")
        return Envelope(data=_fallback_status())
    status = vs.embed_engine_status()
    status["available"] = True
    return Envelope(data=status)


@router.post("/local-deploy/mode", response_model=Envelope[dict])
async def local_deploy_set_mode(request: Request, body: dict) -> Any:
    """切换 embedding 引擎：local=本地模型 / remote=远程 API（热生效 + 持久化）。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    mode = str((body or {}).get("mode", "")).strip().lower()
    if mode not in ("local", "remote"):
        raise HTTPException(status_code=422, detail="mode must be 'local' or 'remote'")
    status = await asyncio.to_thread(vs.set_embed_mode, mode)
    # 持久化规则：手动切模式同样受「引擎已启动 + 节点选本地」约束，
    # 缺任一条件则不持久化 local，重启默认回退 API
    _persist_embed_mode_by_rule(vs)
    logger.info("local_deploy.mode_switched mode={}", mode)
    return Envelope(data=status)


@router.post("/local-deploy/start", response_model=Envelope[dict])
async def local_deploy_start(request: Request) -> Any:
    """启动本地 embedding 引擎：预加载模型（含 NPU 探测），必须先启动再使用。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    status = await asyncio.to_thread(vs.start_local_engine)
    _persist_embed_mode_by_rule(vs)
    return Envelope(data=status)


@router.post("/local-deploy/stop", response_model=Envelope[dict])
async def local_deploy_stop(request: Request) -> Any:
    """停止本地 embedding 引擎：释放 onnxruntime session / NPU 常驻进程。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    status = await asyncio.to_thread(vs.stop_local_engine)
    # 引擎已停止：持久化失效，重启默认走 API
    _persist_embed_mode_by_rule(vs)
    return Envelope(data=status)


@router.get("/local-deploy/model-nodes", response_model=Envelope[list[dict]])
async def local_deploy_model_nodes(request: Request) -> Any:
    """功能节点清单与状态：每个节点的后端选择 + API/本地可用性。

    system 服务节点（主 LLM 除外）——RAG 与系统内部 AI 功能依赖的免费模型入口，
    前端按节点选择「本地模型 / API」。
    """
    from web.local_deploy_nodes import build_status
    core = getattr(request.app.state, "core", None)
    vs = _get_vector_store(request)
    nodes = await build_status(core, vs, get_config_service())
    return Envelope(data=nodes)


@router.put("/local-deploy/model-nodes", response_model=Envelope[dict])
async def local_deploy_set_model_node(request: Request, body: dict) -> Any:
    """设置功能节点的后端选择（local/api），持久化 + 热生效。

    选择 local 时可附 local_model 指定具体本地模型（如已安装的 bge 仓库）。
    """
    from web.local_deploy_nodes import (
        NODES,
        apply_to_runtime,
        ensure_local_instance,
        get_backend,
        get_local_model,
        set_backend,
        stop_node_instance,
        validate_local_selection,
    )
    node_id = str((body or {}).get("node_id", "")).strip()
    backend = str((body or {}).get("backend", "")).strip().lower()
    requested_model = (body or {}).get("local_model")
    requested_model = str(requested_model).strip() if requested_model is not None else None

    async with _NODE_UPDATE_LOCK:
        cfg = get_config_service()
        node = next((item for item in NODES if item["id"] == node_id), None)
        if node is None:
            raise HTTPException(status_code=422, detail=f"unknown model node: {node_id}")
        if backend not in {"local", "api", "off"}:
            raise HTTPException(status_code=422, detail=f"invalid backend: {backend}")

        prev_backend = get_backend(cfg, node_id)
        prev_model = get_local_model(cfg, node_id)
        local_model = requested_model or prev_model or node.get("local_model") or None
        core = getattr(request.app.state, "core", None)
        vs = _get_vector_store(request)
        manager = getattr(core, "local_ai_instances", None) if core is not None else None
        prepared_instance = None
        prepared_new = False
        old_selection = None
        old_generation = None
        committed_generation = None
        selection_purpose = None

        if backend == "local":
            if core is None:
                raise HTTPException(status_code=409, detail="Agent core is not initialized")
            try:
                registry_id = await validate_local_selection(core, node, local_model or "")
                existing = manager.instance_for_model(registry_id) if manager is not None else None
                prepared_instance = await ensure_local_instance(
                    core, node_id, local_model or "", required=True, select=False
                )
                prepared_new = existing is None and prepared_instance is not None
                if node["model_purpose"] in {"embedding", "reranker"}:
                    from local_ai.contracts import ModelPurpose
                    selection_purpose = ModelPurpose(node["model_purpose"])
                    old_selection = manager.selection_identity(selection_purpose)
                    old_generation = manager.selection_generation(selection_purpose)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

        try:
            normalized = set_backend(cfg, node_id, backend, local_model=local_model)
            if prepared_instance is not None and selection_purpose is not None:
                committed_generation = manager.select_instance(
                    selection_purpose, prepared_instance.id,
                    expected_generation=old_generation,
                )
            if core is not None:
                if node_id == "embedding":
                    await asyncio.to_thread(
                        apply_to_runtime, core, vs, node_id, normalized,
                        request.app, local_model, True,
                    )
                else:
                    apply_to_runtime(
                        core, vs, node_id, normalized, app=request.app,
                        local_model=local_model, strict=True,
                    )
        except (OSError, RuntimeError, ValueError, ImportError) as original_error:
            rollback_errors: list[str] = []
            try:
                set_backend(cfg, node_id, prev_backend, local_model=prev_model)
            except Exception as rollback_error:
                rollback_errors.append(f"config: {rollback_error}")
            if manager is not None and selection_purpose is not None \
                    and committed_generation is not None:
                try:
                    previous_id = old_selection[0] if old_selection else None
                    manager.select_instance(
                        selection_purpose, previous_id,
                        expected_generation=committed_generation,
                    )
                except Exception as rollback_error:
                    rollback_errors.append(f"selection: {rollback_error}")
            if core is not None:
                try:
                    apply_to_runtime(
                        core, vs, node_id, prev_backend, app=request.app,
                        local_model=prev_model, strict=False,
                    )
                except Exception as rollback_error:
                    rollback_errors.append(f"runtime: {rollback_error}")
            if prepared_new and manager is not None and prepared_instance is not None:
                try:
                    await manager.stop(prepared_instance.id)
                except Exception as rollback_error:
                    rollback_errors.append(f"cleanup: {rollback_error}")
            detail = f"节点切换失败: {original_error}"
            if rollback_errors:
                detail += f"; 回滚异常: {'; '.join(rollback_errors)}"
            raise HTTPException(status_code=409, detail=detail) from original_error

        if core is not None and prev_backend == "local" and prev_model \
                and (normalized != "local" or prev_model != local_model):
            await stop_node_instance(core, node_id, prev_model, cfg=cfg)
        _persist_embed_mode_by_rule(vs)
        return Envelope(data={
            "node_id": node_id,
            "backend": normalized,
            "local_model": local_model,
            "effective": get_backend(cfg, node_id),
        })


@router.get("/local-deploy/logs", response_model=Envelope[list[str]])
async def local_deploy_logs(request: Request, limit: int = 60, topic: str = "deploy") -> Any:
    """返回本地部署相关日志（agent.log 尾部，按 topic 过滤）。

    topic=deploy  向量嵌入引擎：启动/停止/模式切换/后端选择
    topic=device  算力设备：NPU 探测 / 设备持久化 / 启动应用 / 自适应降级
    """
    from config import LOG_DIR
    log_file = LOG_DIR / "agent.log"
    n = min(max(int(limit), 10), 500)
    tail: list[str] = []
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-500:]
        except OSError:
            tail = []
    if topic == "device":
        keywords = ("npu_probe", "npu_embed", "adaptive_embed",
                    "local_deploy.device", "bootstrap.local_deploy")
    else:
        keywords = ("embed", "npu", "vector_store.local", "adaptive", "local_deploy")
    lines = [ln.rstrip("\n") for ln in tail
             if any(k in ln.lower() for k in keywords)][-n:]
    return Envelope(data=lines)


def _prompt_node_for(prompt_id: str) -> str | None:
    from web.prompt_profiles import NODE_PROMPT_PROFILES

    for node_id, profiles in NODE_PROMPT_PROFILES.items():
        if any(profile.prompt_id == prompt_id for profile in profiles):
            return node_id
    return None


def _get_prompt_repository() -> Any:
    from web.prompt_profile_repository import PromptProfileRepository

    return PromptProfileRepository(get_config_service())


def _prompt_audit() -> Any:
    from web.prompt_audit import get_prompt_audit

    return get_prompt_audit()


@router.get("/local-deploy/prompt-profiles/{prompt_id}/audit",
            response_model=Envelope[list[dict]])
async def get_prompt_audit_log(prompt_id: str, limit: int = 30) -> Any:
    """该提示词的治理事件留痕：ab-run/stage/promote/rollback 摘要（最新在后）。"""
    return Envelope(data=_prompt_audit().recent(limit=min(max(limit, 1), 200),
                                                prompt_id=prompt_id))


def _serialize_golden_case(case: Any) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "variables": dict(case.variables),
        "required_fields": list(case.required_fields),
        "expect_contains": list(case.expect_contains),
        "expect_absent": list(case.expect_absent),
        "evidence_check": bool(case.evidence_quote_field),
    }


@router.get("/local-deploy/prompt-profiles/{node_id}", response_model=Envelope[dict])
async def get_prompt_profiles(node_id: str) -> Any:
    """节点的提示词 profile 概览：版本/哈希/status、golden cases 与 staged override。"""
    from web.node_registry import NODES
    from web.prompt_profiles import profiles_for_node

    if not any(node["id"] == node_id for node in NODES):
        raise HTTPException(status_code=404, detail=f"unknown model node: {node_id}")
    profiles = profiles_for_node(node_id)
    if not profiles:
        raise HTTPException(status_code=404, detail=f"node has no generative prompts: {node_id}")

    repository = _get_prompt_repository()
    cfg = repository._config
    items = []
    for profile in profiles:
        summary = profile.public_summary()
        summary["staged"] = isinstance(
            cfg.get(f"prompt_profiles.staging.{profile.prompt_id}"), dict
        )
        production = cfg.get(f"prompt_profiles.production.{profile.prompt_id}")
        summary["overridden"] = isinstance(production, dict)
        items.append(summary)
    cases = golden_cases_for_node(node_id)
    return Envelope(data={
        "node_id": node_id,
        "profiles": items,
        "golden_cases": [_serialize_golden_case(c) for c in cases],
    })


@router.post("/local-deploy/prompt-profiles/{prompt_id}/stage", response_model=Envelope[dict])
async def stage_prompt_profile(prompt_id: str, body: dict) -> Any:
    try:
        staged = _get_prompt_repository().stage({**(body or {}), "prompt_id": prompt_id})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _prompt_audit().append({
        "event": "stage",
        "prompt_id": prompt_id,
        "version": staged.get("version"),
        "template_hash": staged.get("template_hash"),
    })
    return Envelope(data=staged)


@router.post("/local-deploy/prompt-profiles/{prompt_id}/promote", response_model=Envelope[dict])
async def promote_prompt_profile(request: Request, prompt_id: str, body: dict) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(status_code=400, detail="缺少 X-Confirm: yes 确认头")
    report = (body or {}).get("ab_report")
    if report is not None and not isinstance(report, dict):
        raise HTTPException(status_code=422, detail="ab_report must be an object")
    try:
        promoted = _get_prompt_repository().promote(prompt_id, ab_report=report)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _prompt_audit().append({
        "event": "promote",
        "prompt_id": prompt_id,
        "version": promoted.get("version"),
        "template_hash": promoted.get("template_hash"),
        "gated": report is not None,
    })
    logger.info("prompt_profile.promoted", prompt_id=prompt_id,
                version=promoted.get("version"), gated=report is not None)
    return Envelope(data=promoted)


@router.post("/local-deploy/prompt-profiles/{prompt_id}/rollback", response_model=Envelope[dict])
async def rollback_prompt_profile(request: Request, prompt_id: str) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(status_code=400, detail="缺少 X-Confirm: yes 确认头")
    try:
        previous = _get_prompt_repository().rollback(prompt_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    _prompt_audit().append({
        "event": "rollback",
        "prompt_id": prompt_id,
        "restored_version": previous.get("version"),
    })
    return Envelope(data=previous)


@router.post("/local-deploy/prompt-profiles/{prompt_id}/ab-eval", response_model=Envelope[dict])
async def ab_eval_prompt_profile(prompt_id: str, body: dict) -> Any:
    """同一批 golden cases 上对比 baseline/candidate 两份输出，返回报告与门禁结论。"""
    from web.prompt_ab import compare_runs, promote_gate

    data = body or {}
    node_id = _prompt_node_for(prompt_id)
    if node_id is None:
        raise HTTPException(status_code=404, detail=f"unknown prompt profile: {prompt_id}")
    cases = golden_cases_for_node(node_id)
    if not cases:
        raise HTTPException(status_code=409, detail=f"no golden cases for node: {node_id}")
    baseline = data.get("baseline_outputs")
    candidate = data.get("candidate_outputs")
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise HTTPException(
            status_code=422,
            detail="baseline_outputs and candidate_outputs must be objects keyed by case_id",
        )
    report = compare_runs(
        cases,
        {str(k): str(v) for k, v in baseline.items()},
        {str(k): str(v) for k, v in candidate.items()},
        baseline_label=str(data.get("baseline_label") or "baseline"),
        candidate_label=str(data.get("candidate_label") or "candidate"),
    )
    passed, reasons = promote_gate(report)
    return Envelope(data={
        "prompt_id": prompt_id,
        "node_id": node_id,
        "report": report,
        "gate": {"passed": passed, "reasons": reasons},
    })


@router.post("/local-deploy/prompt-profiles/{prompt_id}/ab-run", response_model=Envelope[dict])
async def ab_run_prompt_profile(request: Request, prompt_id: str, body: dict | None = None) -> Any:
    """真实模型执行 golden cases：内置模板 vs staged override，产出报告与门禁结论。

    body.backends 可选 current/api/local（默认 current）；多后端时逐路独立
    跑分并要求门禁全部通过（本地/API 分开评分）。
    """
    from web.prompt_ab_runner import (
        SWEEP_BACKENDS,
        run_prompt_ab,
        run_prompt_ab_multi,
        run_prompt_ab_sweep,
    )
    from web.prompt_profiles import profile_by_id

    profile = profile_by_id(prompt_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown prompt profile: {prompt_id}")
    if not profile.template_refs:
        raise HTTPException(
            status_code=409,
            detail=f"prompt not bound to builtin templates; cannot auto-run: {prompt_id}",
        )
    data = body or {}
    backends = data.get("backends")
    if backends is None:
        backends = ["current"]
    if not isinstance(backends, list) or not backends or \
            not all(b in SWEEP_BACKENDS for b in backends):
        raise HTTPException(
            status_code=422,
            detail=f"backends must be a non-empty subset of {list(SWEEP_BACKENDS)}",
        )
    raw_runs = data.get("runs")
    if raw_runs is None:
        runs = 1
    else:
        # 严格校验：0/""/bool 等 falsy 值不得被 `or 1` 吞成合法轮次
        if isinstance(raw_runs, bool) or not isinstance(raw_runs, int):
            raise HTTPException(status_code=422, detail="runs must be an integer")
        runs = raw_runs
    if runs < 1 or runs > 5:
        raise HTTPException(status_code=422, detail="runs must be within 1..5")
    if len(backends) > 1 and runs > 1:
        raise HTTPException(
            status_code=422,
            detail="runs>1 with multiple backends is not supported yet; "
                   "run each backend separately",
        )
    core = getattr(request.app.state, "core", None)
    node_local_model = None
    if "current" in backends or "local" in backends:
        if core is None or getattr(core, "router", None) is None:
            raise HTTPException(status_code=409, detail="Agent core/router is not initialized")
    if "local" in backends:
        cfg = get_config_service()
        node_local_model = cfg.get(f"local_deploy.node_models.{_prompt_node_for(prompt_id) or ''}")
        node_local_model = str(node_local_model) if node_local_model else None
    repository = _get_prompt_repository()
    ab_lock = _AB_RUN_LOCKS.setdefault(prompt_id, asyncio.Lock())
    if ab_lock.locked():
        raise HTTPException(status_code=409, detail="prompt AB run already in progress")
    try:
        async with ab_lock:
            if len(backends) == 1 and runs > 1:
                result = await run_prompt_ab_multi(
                    core, repository, prompt_id, runs=runs, backend=backends[0],
                    node_local_model=node_local_model,
                )
            elif len(backends) == 1 and backends[0] == "current":
                result = await run_prompt_ab(core, repository, prompt_id)
            else:
                result = await run_prompt_ab_sweep(
                    core, repository, prompt_id, tuple(backends),
                    node_local_model=node_local_model,
                )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except RuntimeError as e:
        _prompt_audit().append({
            "event": "ab_run_infra_failure",
            "prompt_id": prompt_id,
            "backends": backends,
            "runs": runs,
            "error": str(e),
        })
        raise HTTPException(status_code=409, detail=str(e)) from e
    _prompt_audit().append({
        "event": "ab_run",
        "prompt_id": prompt_id,
        "backends": backends,
        "runs": runs,
        "gate_passed": result["gate"]["passed"],
        "gate_reasons": result["gate"]["reasons"],
        "rates": {
            s["backend"]: {
                "baseline_schema": s["report"]["baseline"]["schema_rate"],
                "candidate_schema": s["report"]["candidate"]["schema_rate"],
                "baseline_golden": s["report"]["baseline"]["golden_rate"],
                "candidate_golden": s["report"]["candidate"]["golden_rate"],
            } for s in (result["sweeps"] if "sweeps" in result else [result])
        },
    })
    logger.info("prompt_profile.ab_run", prompt_id=prompt_id,
                backends=backends, runs=runs, gate_passed=result["gate"]["passed"])
    return Envelope(data=result)
