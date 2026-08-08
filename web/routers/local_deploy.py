"""本地部署路由：向量嵌入引擎管理（API / 本地模型切换、启动/停止、日志）。

WebUI 侧边栏"本地部署"页：选择向量嵌入引擎——远程 API（硅基流动）
或内置本地 BGE 模型（NPU/CPU）。使用本地模型前必须先"启动"，
页面下方展示启动/运行日志。
"""
from __future__ import annotations
import platform
import re
import time
from typing import Any

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user
from web.config_service import get_config_service

router = APIRouter(tags=["local-deploy"], dependencies=[Depends(get_current_user)])

# 设备探测缓存：5 分钟有效，避免每次刷新页面都 spawn runner 探测 NPU
_DEVICE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_DEVICE_CACHE_TTL = 300.0


def _detect_cpu_model() -> str:
    """读取 CPU 型号：Linux 依次尝试 model name / Hardware / 设备树 / Processor。"""
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
                    pass
        except OSError:
            pass
    return platform.processor() or "CPU"


def _detect_gpu_model() -> str:
    """检测 GPU：Linux 用 lspci 查 VGA/3D 控制器，无 lspci 返回空串（未检测到）。"""
    if platform.system() != "Linux":
        return ""
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


def _detect_devices() -> dict:
    """探测本机算力设备（带 5 分钟缓存）。

    返回：
        current: 当前持久化的设备（"" = 跟随 LOCAL_EMBED_BACKEND / auto）
        devices: [{id, name, model, desc, available, active}] —— CPU 恒可用；
                 NPU 由 probe_npu 实测；GPU 恒置灰（"此模型暂不支持"）。
    """
    now = time.monotonic()
    if _DEVICE_CACHE["data"] is not None and (now - _DEVICE_CACHE["ts"]) < _DEVICE_CACHE_TTL:
        return _DEVICE_CACHE["data"]

    # NPU 实测（probe_npu 会 sudo -n 拉起 runner --probe，约 50ms~15s）
    npu_ok = False
    npu_model = ""
    try:
        from memory.npu_embed import probe_npu
        npu_ok = probe_npu()
        if npu_ok:
            npu_model = "Vivante VIP9000 (3 TOPS INT8)"
    except Exception as e:  # noqa: BLE001
        logger.warning("local_deploy.device_npu_probe_failed error={}", str(e))
        npu_ok = False

    cpu_model = _detect_cpu_model()
    gpu_model = _detect_gpu_model()

    # 当前持久化的设备（webui_overrides.json local_deploy.device）
    current = ""
    try:
        current = str(get_config_service().get("local_deploy.device", "") or "")
    except Exception:  # noqa: BLE001
        current = ""

    devices = [
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
            "model": npu_model or "未检测到 VIP9000",
            "desc": "NPU 常驻流加速，短文本 CPU / 长文本 NPU 自适应" if npu_ok
                    else "未检测到可用 NPU（需 Linux + VIP9000 驱动 + sudo 免密）",
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


@router.get("/local-deploy/devices", response_model=Envelope[dict])
async def local_deploy_devices(request: Request) -> Any:
    """算力设备检测：CPU / NPU / GPU 探测结果 + 当前持久化设备。"""
    data = _detect_devices()
    data["runtime_backend"] = os.getenv("LOCAL_EMBED_BACKEND", "auto")
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
        if not probe_npu():
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
    get_config_service().set("local_deploy.mode", mode)
    logger.info("local_deploy.mode_switched mode={}", mode)
    return Envelope(data=status)


@router.post("/local-deploy/start", response_model=Envelope[dict])
async def local_deploy_start(request: Request) -> Any:
    """启动本地 embedding 引擎：预加载模型（含 NPU 探测），必须先启动再使用。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    status = await asyncio.to_thread(vs.start_local_engine)
    get_config_service().set("local_deploy.mode", "local")
    return Envelope(data=status)


@router.post("/local-deploy/stop", response_model=Envelope[dict])
async def local_deploy_stop(request: Request) -> Any:
    """停止本地 embedding 引擎：释放 onnxruntime session / NPU 常驻进程。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    status = await asyncio.to_thread(vs.stop_local_engine)
    return Envelope(data=status)


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
