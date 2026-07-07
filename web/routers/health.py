"""健康测试中心路由（R12）：LLM/TTS/视频/MCP/DB/向量 探针、系统信息、报告。"""
from __future__ import annotations
from typing import Any

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["health"], dependencies=[Depends(get_current_user)])

_all_running = False


@router.get("/health/self", response_model=Envelope[dict])
async def agent_self(request: Request) -> Any:
    """Agent 状态自省 — 返回当前内心状态 (认知负载/置信度/情绪/降级级别/健康度)

    内部状态, 不需要单独认证 (已有 router 级 Depends)。
    """
    from core.agent_introspection import AgentIntrospector

    core = request.app.state.core
    # 复用全局 introspector (若有), 否则用 core/context 现场构造
    introspector = AgentIntrospector(
        context=getattr(core, "context", None),
        agent=core,
    )
    state = introspector.get_current_state()
    return Envelope(data=introspector.to_dict(state))


@router.get("/health/probes", response_model=Envelope[list[dict]])
async def list_probes(request: Request) -> Any:
    from web.probes import list_probe_ids
    return Envelope(data=list_probe_ids(request.app.state.core))


@router.post("/health/test/llm", response_model=Envelope[dict])
async def test_llm(body: dict, request: Request) -> Any:
    from web.probes import probe_llm
    core = request.app.state.core
    provider_id = body.get("provider_id")
    if provider_id:
        return Envelope(data=await _probe_provider(request, provider_id))
    return Envelope(data=await probe_llm(core, body.get("route", "chat")))


async def _probe_provider(request: Request, provider_id: str) -> dict:
    """对自定义 provider 直连探活（委托给 probes.probe_provider）。"""
    from web.probes import probe_provider
    return await probe_provider(request.app.state.core, provider_id)


@router.post("/health/test/tts", response_model=Envelope[dict])
async def test_tts(request: Request) -> Any:
    from web.probes import probe_tts
    return Envelope(data=await probe_tts(request.app.state.core))


@router.post("/health/test/video", response_model=Envelope[dict])
async def test_video(request: Request) -> Any:
    from web.probes import probe_video_config
    return Envelope(data=await probe_video_config())


@router.post("/health/test/mcp/{server}", response_model=Envelope[dict])
async def test_mcp(server: str, request: Request) -> Any:
    from web.probes import probe_mcp
    return Envelope(data=await probe_mcp(request.app.state.core, server))


@router.post("/health/test/{probe_id:path}", response_model=Envelope[dict])
async def test_one(probe_id: str, request: Request) -> Any:
    from web.probes import run_probe
    return Envelope(data=await run_probe(request.app.state.core, probe_id))


@router.post("/health/test-all", response_model=Envelope[dict])
async def test_all(request: Request) -> Any:
    """一键全检：后台串行执行，逐项进度走 WS health_progress。"""
    global _all_running
    if _all_running:
        raise HTTPException(409, "全量自检已在进行中")
    core = request.app.state.core
    _all_running = True  # 提前设置，防止 TOCTOU 竞态

    async def _run() -> None:
        global _all_running
        try:
            from web.probes import run_all
            from web.ws_hub import manager

            async def on_progress(item_id: str, res: dict) -> None:
                await manager.broadcast({
                    "type": "health_progress", "item": item_id,
                    "ok": res.get("ok", False),
                    "detail": res.get("error") or res.get("reply_excerpt") or "",
                    "latency_ms": res.get("latency_ms", 0),
                })

            report = await run_all(core, on_progress=on_progress)
            await manager.broadcast({
                "type": "health_done",
                "passed": report["passed"], "total": report["total"],
            })
        except Exception as e:
            logger.warning("health.run_all_failed error={}", str(e))
        finally:
            _all_running = False

    asyncio.create_task(_run())
    return Envelope(data={"started": True})


@router.get("/health/report", response_model=Envelope[dict])
async def last_report(request: Request) -> Any:
    core = request.app.state.core
    row = await core.db.fetch_one(
        "SELECT * FROM health_reports ORDER BY run_at DESC LIMIT 1")
    if not row:
        return Envelope(data={})
    try:
        row["detail"] = json.loads(row["detail"])
    except Exception:
        pass
    return Envelope(data=row)


@router.get("/health/system", response_model=Envelope[dict])
async def system_info() -> Any:
    import platform
    data: dict = {"timestamp": time.time(), "platform": platform.system()}

    try:
        import psutil
    except ImportError as e:
        logger.error("health.psutil_import_failed error={}", str(e))
        data["error"] = f"psutil导入失败: {str(e)}"
        return Envelope(data=data)
    except Exception as e:
        logger.error("health.psutil_init_failed error={}", str(e))
        data["error"] = f"psutil初始化失败: {str(e)}"
        return Envelope(data=data)

    data.update(_collect_cpu_mem_metrics(psutil))
    data.update(_collect_disk_temp_metrics(psutil))
    data.update(_collect_proc_net_metrics(psutil))
    return Envelope(data=data)


def _collect_cpu_mem_metrics(psutil: Any) -> dict:
    """收集 CPU、内存、交换区、负载指标。"""
    data: dict = {}
    # ── CPU ──
    try:
        data["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        data["cpu_count"] = psutil.cpu_count(logical=True)
        data["cpu_count_physical"] = psutil.cpu_count(logical=False)
    except Exception as e:
        logger.warning("health.cpu_failed error={}", str(e))
    try:
        load1, load5, load15 = os.getloadavg()
        data["load"] = [load1, load5, load15]
    except (AttributeError, OSError):
        pass

    # ── 内存 ──
    try:
        mem = psutil.virtual_memory()
        data["mem_total"] = mem.total
        data["mem_available"] = mem.available
        data["mem_percent"] = mem.percent
    except Exception as e:
        logger.warning("health.mem_failed error={}", str(e))

    # ── 交换区 ──
    try:
        swap = psutil.swap_memory()
        data["swap_total"] = swap.total
        data["swap_used"] = swap.used
        data["swap_percent"] = swap.percent
    except Exception as e:
        logger.warning("health.swap_failed error={}", str(e))
    return data


def _collect_disk_temp_metrics(psutil: Any) -> dict:
    """收集磁盘分区和温度传感器指标。"""
    data: dict = {}
    # ── 磁盘（所有分区）──
    try:
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue
        data["disks"] = disks
    except Exception as e:
        logger.warning("health.disks_failed error={}", str(e))

    # ── 温度（Windows 不支持 sensors_temperatures）──
    try:
        if hasattr(psutil, 'sensors_temperatures'):
            temps = psutil.sensors_temperatures()
            temp_list = []
            for name, entries in temps.items():
                for entry in entries:
                    temp_list.append({
                        "label": entry.label or name,
                        "current": entry.current,
                        "high": entry.high,
                        "critical": entry.critical,
                    })
            data["temperatures"] = temp_list
        else:
            data["temperatures"] = []
    except Exception as e:
        logger.warning("health.temps_failed error={}", str(e))
        data["temperatures"] = []
    return data


def _collect_proc_net_metrics(psutil: Any) -> dict:
    """收集运行时间、进程内存、网络、电池指标。"""
    data: dict = {}
    # ── 运行时间 ──
    try:
        data["uptime"] = time.time() - psutil.boot_time()
    except Exception as e:
        logger.warning("health.uptime_failed error={}", str(e))

    # ── 进程内存 ──
    try:
        proc = psutil.Process()
        mem_info = proc.memory_info()
        data["process_rss"] = mem_info.rss
        data["process_vms"] = mem_info.vms
    except Exception as e:
        logger.warning("health.process_mem_failed error={}", str(e))

    # ── 网络 ──
    try:
        net = psutil.net_io_counters()
        data["net_bytes_sent"] = net.bytes_sent
        data["net_bytes_recv"] = net.bytes_recv
    except Exception as e:
        logger.warning("health.net_failed error={}", str(e))

    # ── 电池（笔记本）──
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            data["battery_percent"] = bat.percent
            data["battery_plugged"] = bat.power_plugged
            data["battery_secs_left"] = bat.secsleft
    except Exception as e:
        logger.warning("health.battery_failed error={}", str(e))
    return data
