"""健康测试中心路由（R12）：LLM/TTS/视频/MCP/DB/向量 探针、系统信息、报告。"""
from __future__ import annotations

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


@router.get("/health/probes", response_model=Envelope[list[dict]])
async def list_probes(request: Request):
    from web.probes import list_probe_ids
    return Envelope(data=list_probe_ids(request.app.state.core))


@router.post("/health/test/llm", response_model=Envelope[dict])
async def test_llm(body: dict, request: Request):
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
async def test_tts(request: Request):
    from web.probes import probe_tts
    return Envelope(data=await probe_tts(request.app.state.core))


@router.post("/health/test/video", response_model=Envelope[dict])
async def test_video(request: Request):
    from web.probes import probe_video_config
    return Envelope(data=await probe_video_config())


@router.post("/health/test/mcp/{server}", response_model=Envelope[dict])
async def test_mcp(server: str, request: Request):
    from web.probes import probe_mcp
    return Envelope(data=await probe_mcp(request.app.state.core, server))


@router.post("/health/test/{probe_id:path}", response_model=Envelope[dict])
async def test_one(probe_id: str, request: Request):
    from web.probes import run_probe
    return Envelope(data=await run_probe(request.app.state.core, probe_id))


@router.post("/health/test-all", response_model=Envelope[dict])
async def test_all(request: Request):
    """一键全检：后台串行执行，逐项进度走 WS health_progress。"""
    global _all_running
    if _all_running:
        raise HTTPException(409, "全量自检已在进行中")
    core = request.app.state.core

    async def _run():
        global _all_running
        _all_running = True
        try:
            from web.probes import run_all
            from web.ws_hub import manager

            async def on_progress(item_id: str, res: dict):
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
async def last_report(request: Request):
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
async def system_info():
    import platform
    import subprocess
    data: dict = {"timestamp": time.time(), "platform": platform.system()}

    # ── CPU 负载 ──
    try:
        load1, load5, load15 = os.getloadavg()
        data["load"] = [load1, load5, load15]
    except OSError:
        pass
    if "load" not in data and platform.system() == "Windows":
        try:
            # Windows: 用 wmic 获取 CPU 使用率
            out = subprocess.check_output(
                "wmic cpu get loadpercentage /value", shell=True, timeout=5
            ).decode(errors="ignore")
            for line in out.strip().splitlines():
                if line.startswith("LoadPercentage="):
                    pct_val = int(line.split("=", 1)[1])
                    data["load"] = [pct_val / 100.0, 0, 0]
                    break
        except Exception:
            pass
    data["cpu_count"] = os.cpu_count()

    # ── 内存 ──
    try:
        meminfo = Path("/proc/meminfo").read_text()
        mem = {}
        for line in meminfo.splitlines()[:5]:
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0]) * 1024
        data["mem_total"] = mem.get("MemTotal", 0)
        data["mem_available"] = mem.get("MemAvailable", 0)
    except Exception:
        pass
    if "mem_total" not in data and platform.system() == "Windows":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            data["mem_total"] = stat.ullTotalPhys
            data["mem_available"] = stat.ullAvailPhys
        except Exception:
            pass

    # ── 磁盘 ──
    try:
        st = os.statvfs("/")
        data["disk_total"] = st.f_blocks * st.f_frsize
        data["disk_free"] = st.f_bavail * st.f_frsize
    except (AttributeError, OSError):
        pass
    if "disk_total" not in data and platform.system() == "Windows":
        try:
            import ctypes
            free_bytes = ctypes.c_ulonglong(0)
            total_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                None, ctypes.pointer(free_bytes),
                ctypes.pointer(total_bytes), None)
            data["disk_total"] = total_bytes.value
            data["disk_free"] = free_bytes.value
        except Exception:
            pass

    # ── 温度 ──
    temps = []
    try:
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                t = int((zone / "temp").read_text().strip()) / 1000.0
                name = (zone / "type").read_text().strip()
                temps.append({"zone": name, "temp_c": round(t, 1)})
            except Exception:
                continue
    except Exception:
        pass
    data["temperatures"] = temps

    # ── 运行时间 ──
    try:
        data["uptime"] = float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        pass
    if "uptime" not in data and platform.system() == "Windows":
        try:
            import ctypes
            data["uptime"] = ctypes.windll.kernel32.GetTickCount64() / 1000.0
        except Exception:
            pass

    # ── 进程内存 ──
    try:
        status = Path("/proc/self/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                data["process_rss"] = int(line.split()[1]) * 1024
                break
    except Exception:
        pass
    if "process_rss" not in data and platform.system() == "Windows":
        try:
            import psutil
            data["process_rss"] = psutil.Process().memory_info().rss
        except Exception:
            pass

    return Envelope(data=data)
