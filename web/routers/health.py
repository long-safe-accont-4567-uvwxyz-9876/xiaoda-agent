"""健康测试中心路由（R12）：LLM/TTS/视频/MCP/DB/向量 探针、系统信息、报告。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["health"], dependencies=[Depends(get_current_user)])

_all_running = False
_all_running_lock = asyncio.Lock()


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
    return Envelope(data=list_probe_ids(request.app.state.core, request.app.state.provider_service))


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
    return await probe_provider(request.app.state.core, provider_id, request.app.state.provider_service)


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
    return Envelope(data=await run_probe(request.app.state.core, probe_id, request.app.state.provider_service))


@router.post("/health/test-all", response_model=Envelope[dict])
async def test_all(request: Request) -> Any:
    """一键全检：后台串行执行，逐项进度走 WS health_progress。"""
    global _all_running
    if _all_running_lock.locked():
        raise HTTPException(409, "全量自检已在进行中")
    core = request.app.state.core
    _all_running = True

    async def _run() -> None:
        global _all_running
        await _all_running_lock.acquire()
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

            report = await run_all(
                core,
                on_progress=on_progress,
                provider_service=request.app.state.provider_service,
            )
            await manager.broadcast({
                "type": "health_done",
                "passed": report["passed"], "total": report["total"],
            })
        except (OSError, RuntimeError, ValueError, ConnectionError) as e:
            logger.warning("health.run_all_failed error={}", str(e))
        except Exception:
            logger.exception("health.on_progress.unexpected_error")
        finally:
            _all_running = False
            try:
                _all_running_lock.release()
            except RuntimeError:
                pass

    request.app.state.health_run_task = asyncio.create_task(_run())
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
    except (json.JSONDecodeError, TypeError):
        logger.debug("health.detail_parse_failed", exc_info=True)
    return Envelope(data=row)


@router.get("/health/system", response_model=Envelope[dict])
async def system_info() -> Any:
    import platform
    data: dict = {"timestamp": time.time(), "platform": platform.system()}

    try:
        import psutil
    except ImportError as e:
        logger.error("health.psutil_import_failed error={}", str(e))
        data["error"] = f"psutil导入失败: {e!s}"
        return Envelope(data=data)
    except (OSError, RuntimeError) as e:
        logger.error("health.psutil_init_failed error={}", str(e))
        data["error"] = f"psutil初始化失败: {e!s}"
        return Envelope(data=data)
    except Exception:
        logger.exception("health.system_info.unexpected_error")
        return Envelope(data=data)
    # 三个收集器含同步子进程探测（nvidia-smi/lspci 各至多 3s）与
    # psutil.cpu_percent(interval=0.5) 的 500ms 阻塞——统一下放线程池，
    # 避免仪表盘 5s 轮询周期性卡住事件循环。
    data.update(await asyncio.to_thread(_collect_cpu_mem_metrics, psutil))
    data.update(await asyncio.to_thread(_collect_disk_temp_metrics, psutil))
    data.update(await asyncio.to_thread(_collect_proc_net_metrics, psutil))
    return Envelope(data=data)


def _collect_cpu_mem_metrics(psutil: Any) -> dict:
    """收集 CPU、内存、交换区、负载指标。"""
    data: dict = {}
    # ── CPU ──
    try:
        data["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        data["cpu_count"] = psutil.cpu_count(logical=True)
        data["cpu_count_physical"] = psutil.cpu_count(logical=False)
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.cpu_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_cpu_mem_metrics.unexpected_error")
    try:
        load1, load5, load15 = os.getloadavg()
        data["load"] = [load1, load5, load15]
    except (AttributeError, OSError):
        logger.debug("health.load_avg_unavailable", exc_info=True)

    # ── 内存 ──
    try:
        mem = psutil.virtual_memory()
        data["mem_total"] = mem.total
        data["mem_available"] = mem.available
        data["mem_percent"] = mem.percent
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.mem_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_cpu_mem_metrics.unexpected_error")
    # ── 交换区 ──
    try:
        swap = psutil.swap_memory()
        data["swap_total"] = swap.total
        data["swap_used"] = swap.used
        data["swap_percent"] = swap.percent
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.swap_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_cpu_mem_metrics.unexpected_error")
    return data


_gpu_probe_cache: dict = {"ts": 0.0, "present": False}
_GPU_PROBE_TTL = 60.0  # 秒；仪表盘约每 5s 轮询一次，探测结果短缓存避免频繁起子进程


def _system_has_gpu() -> bool:
    """真实探测主机是否有可用 GPU（短 TTL 缓存，尽量零开销）。

    判定顺序（任一命中即视为有 GPU）：
      1) nvidia-smi 可执行且能列出 GPU（驱动在装但无卡时 nvidia-smi -L 会失败）
      2) rocm-smi 可执行（AMD ROCm 存在）
      3) lspci 存在 VGA/3D/Display 显示控制器（Intel/独显直通等）
    全部不命中 → 视为无 GPU（SoC 芯片内的 gpu_thermal_zone 不是独立显卡）。
    """
    now = time.monotonic()
    if now - _gpu_probe_cache["ts"] < _GPU_PROBE_TTL:
        return _gpu_probe_cache["present"]

    present = False
    try:
        nvidia = shutil.which("nvidia-smi")
        if nvidia:
            try:
                # 无卡时 nvidia-smi -L 退出码非 0 且无输出，可区分"装了驱动但无卡"
                proc = subprocess.run(
                    [nvidia, "-L"], capture_output=True, text=True, timeout=3
                )
                present = proc.returncode == 0 and bool(proc.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                present = False
        if not present and shutil.which("rocm-smi"):
            present = True  # AMD GPU 驱动存在（rocm-smi 与卡强绑定，装上即有 amdgpu）
        if not present and shutil.which("lspci"):
            try:
                proc = subprocess.run(
                    ["lspci", "-nn"], capture_output=True, text=True, timeout=3
                )
                out = proc.stdout.lower()
                present = any(
                    kw in out
                    for kw in ("vga compatible", "3d controller", "display controller")
                )
            except (OSError, subprocess.SubprocessError):
                present = False
    except (OSError, subprocess.SubprocessError) as e:
        # 外层兜底收窄（批次 A）：内部已逐段窄捕获，此处只可能漏出
        # which/exec 环境类异常；原 except Exception 属过度宽泛
        logger.debug("health.gpu_probe_failed error={}", str(e))
        present = False

    _gpu_probe_cache.update(ts=now, present=present)
    return present


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
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.disks_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_disk_temp_metrics.unexpected_error")
    # ── 温度（仪表盘展示：内核 zone 名映射为可读中文标签）──
    # Linux 下 psutil 返回的是内核 sensor 名（如 gpu_thermal_zone）。
    # gpu_thermal_zone 不是"存在独立显卡"的证据 —— 很多 SoC 主板上它只是
    # 芯片内微型图形核心的温度。因此 GPU 相关 zone 的展示名不写死：
    # 先真实探测主机是否有可用的 GPU（nvidia-smi / rocm-smi / lspci 显示类
    # 设备），有才标 "GPU"，没有则用中性名「SoC(芯片)」，避免把无独显的
    # 机器误读成有显卡。
    # *_idle_zone 与同名主 zone 读数重复，直接跳过避免仪表盘重复展示。
    gpu_label = "GPU(芯片集成)" if _system_has_gpu() else "SoC(芯片)"
    _TEMP_ZONE_LABELS = {
        "cpub_thermal_zone": "CPU·大核",
        "cpul_thermal_zone": "CPU·小核",
        "gpu_thermal_zone": gpu_label,
        "npu_thermal_zone": "NPU(芯片)",
        "ddr_thermal_zone": "DDR 内存",
        "skin_zone": "机身",
        "soc_thermal": "SoC",
        "trip_point_0_thermal": "SoC",
        "coretemp": "CPU",
        "k10temp": "CPU",
        "cpu_thermal": "CPU",
        "gpu_temp_sensor": gpu_label,
    }
    try:
        if hasattr(psutil, 'sensors_temperatures'):
            temps = psutil.sensors_temperatures()
            temp_list = []
            for name, entries in temps.items():
                if name.endswith("_idle_zone"):
                    continue  # 与同名单区重复读数
                for entry in entries:
                    temp_list.append({
                        "label": _TEMP_ZONE_LABELS.get(name, entry.label or name),
                        "current": entry.current,
                        "high": entry.high,
                        "critical": entry.critical,
                    })
            data["temperatures"] = temp_list
        else:
            data["temperatures"] = []
    except (OSError, RuntimeError, ValueError, AttributeError) as e:
        logger.warning("health.temps_failed error={}", str(e))
        data["temperatures"] = []
    except Exception:
        logger.exception("health.temps.unexpected_error")
    return data


def _collect_proc_net_metrics(psutil: Any) -> dict:
    """收集运行时间、进程内存、网络、电池指标。"""
    data: dict = {}
    # ── 运行时间 ──
    try:
        data["uptime"] = time.time() - psutil.boot_time()
    except (OSError, RuntimeError) as e:
        logger.warning("health.uptime_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_proc_net_metrics.unexpected_error")
    # ── 进程内存 ──
    try:
        proc = psutil.Process()
        mem_info = proc.memory_info()
        data["process_rss"] = mem_info.rss
        data["process_vms"] = mem_info.vms
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.process_mem_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_proc_net_metrics.unexpected_error")
    # ── 网络 ──
    try:
        net = psutil.net_io_counters()
        data["net_bytes_sent"] = net.bytes_sent
        data["net_bytes_recv"] = net.bytes_recv
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.net_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_proc_net_metrics.unexpected_error")
    # ── 电池（笔记本）──
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            data["battery_percent"] = bat.percent
            data["battery_plugged"] = bat.power_plugged
            data["battery_secs_left"] = bat.secsleft
    except (OSError, RuntimeError, ValueError, AttributeError) as e:
        logger.warning("health.battery_failed error={}", str(e))
    except Exception:
        logger.exception("health._collect_proc_net_metrics.unexpected_error")
    return data


# ── 真实 /health 探活（无认证、O(1)、供外部探针高频调用）──────────────────
# 背景：/health 与 /healthz 此前没有注册任何路由，请求会落到 web/server.py 中
# 挂载在 "/" 的 NoCacheHTMLStaticFiles（SPA fallback），返回 200 + index.html，
# 依赖全挂时探针依然是绿的。本 router 挂在 app 根（不带 /api/v1 前缀），并在
# web/server.py::_register_routes 内先于 static mount 注册以保证路由优先级。
#
# 三态语义（探针契约）：
#   ok        → HTTP 200, status="ok"        全部依赖健康
#   degraded  → HTTP 200, status="degraded"  依赖降级但仍可服务（嵌入引擎未就绪、
#                                            检索退化为 FTS 关键词、可写性未判定）
#   unhealthy → HTTP 503, status="unhealthy" 服务级故障（数据库不可用、探测超时/异常）
# 为什么降级不返回 503：外部探针（反代、uptime 监控、k8s）只会读状态码，
# 把「检索还能降级到关键词」报成 503，等于让它们把还能聊天的服务当宕机重启。
#
# 开销约束：每次探测只做一次 SELECT 1、一次可写性检查（真实临时文件，60s 缓存）、
# 一次内存态读取。不取写锁（真写事务会与对话写路径争用 _write_tx_lock，反而制造
# 假故障），不做 KNN 检索。整体硬超时 1.5s，超时按不健康上报。
probe_router = APIRouter(tags=["health"], include_in_schema=False)

_HEALTH_PROBE_TIMEOUT = 1.5

# 可写性用真实创建/删除临时文件验证：os.access(W_OK) 在 root 身份下恒真（只看
# 权限位），只读挂载也可能被权限位掩盖，唯一可靠的判据是实际写一次。但匿名探针
# 若按秒级频率打过来，每次都建文件会放大写压力，所以结果按目录缓存 60s。
_WRITE_PROBE_TTL = 60.0
_write_probe_cache: dict[str, tuple[float, dict]] = {}


def _check(state: str, reason: str | None = None, **extra: Any) -> dict:
    """构造单项检查结果：state 是三态判据，ok 只是 state == "ok" 的便利字段。"""
    result: dict = {"state": state, "ok": state == "ok"}
    if reason:
        result["reason"] = reason
    result.update(extra)
    return result


def _probe_db_writability(db: Any) -> dict:
    """数据库目录可写性：真实写入探针 + 60s 缓存。

    state：writable / unwritable / skipped（取不到 db.db_path 时）。
    skipped 不算 ok：拿不到路径说明 DB 封装的隐式契约破了，这一项无法证伪，
    上报为 degraded 让人看得见，但不至于把服务判死。
    """
    raw_path = getattr(db, "db_path", None)
    if not raw_path:
        return {"state": "skipped", "writable": None, "source": "db.db_path",
                "detail": "db.db_path unavailable"}
    try:
        target_dir = Path(str(raw_path)).parent
    except (OSError, TypeError, ValueError) as e:
        return {"state": "skipped", "writable": None, "source": "db.db_path",
                "detail": "%s: %s" % (type(e).__name__, str(e)[:80])}
    key = str(target_dir)
    now = time.monotonic()
    cached = _write_probe_cache.get(key)
    if cached is not None and now - cached[0] < _WRITE_PROBE_TTL:
        return dict(cached[1])
    tmp_name = None
    result: dict
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".health-w-", suffix=".tmp",
                                        dir=str(target_dir))
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
        result = {"state": "writable", "writable": True, "source": "tempfile",
                  "detail": key}
    except (OSError, TypeError, ValueError) as e:
        result = {"state": "unwritable", "writable": False, "source": "tempfile",
                  "detail": "%s: %s" % (type(e).__name__, str(e)[:120])}
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    _write_probe_cache[key] = (now, dict(result))
    return result


async def _probe_database(core: Any) -> dict:
    """数据库探测：连接存活（真实查询）+ 数据库文件可写性。

    连接层异常 = unhealthy（读写会话/记忆全废，属服务级故障）；
    可写性确认失败 = unhealthy；可写性无法判定 = degraded。
    """
    t0 = time.perf_counter()
    db = getattr(core, "db", None) if core is not None else None
    if db is None:
        return _check("unhealthy", "database not initialized", latency_ms=0)
    try:
        await db.fetch_one("SELECT 1 AS ok")
    except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as e:
        # 白名单外的东西由 _guarded 兜成 unhealthy JSON，不会漏成 500 纯文本
        return _check("unhealthy", "%s: %s" % (type(e).__name__, str(e)[:120]),
                      latency_ms=int((time.perf_counter() - t0) * 1000))
    writability = _probe_db_writability(db)
    base = {
        "writable": writability.get("writable"),
        "writable_state": writability.get("state"),
        "writable_source": writability.get("source"),
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }
    if writability.get("state") == "unwritable":
        return _check("unhealthy",
                      "database not writable: %s" % writability.get("detail"), **base)
    if writability.get("state") == "skipped":
        return _check("degraded",
                      "writability check skipped: %s" % writability.get("detail"), **base)
    return _check("ok", None, **base)


def _probe_embedding(core: Any) -> dict:
    """嵌入引擎探测：只读内存态（MemoryManager.vec.embed_engine_status）。

    嵌入/检索属功能面：引擎未就绪、API key 未配置、向量库缺失都只降级为
    degraded（HTTP 200）——此时服务仍能对话，只是检索退化到关键词。
    """
    t0 = time.perf_counter()
    memory = getattr(core, "memory", None) if core is not None else None
    vector_store = getattr(memory, "vec", None) if memory is not None else None
    if vector_store is None:
        return _check("degraded", "vector store not initialized", ready=False,
                      latency_ms=0)
    try:
        status = vector_store.embed_engine_status()
    except (OSError, RuntimeError, ValueError, AttributeError, TypeError) as e:
        return _check("degraded", "%s: %s" % (type(e).__name__, str(e)[:120]),
                      ready=False, latency_ms=int((time.perf_counter() - t0) * 1000))
    mode = status.get("mode")
    ready = bool(status.get("engine_running"))
    api_configured = bool(status.get("api_configured"))
    detail = {
        "ready": ready,
        "mode": mode,
        "backend": status.get("backend"),
        "dimensions": status.get("dimensions"),
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }
    if mode == "local":
        if ready:
            return _check("ok", None, **detail)
        return _check("degraded", "local embedding engine not ready", **detail)
    if api_configured:
        return _check("ok", None, **detail)
    return _check("degraded", "embedding api key not configured", **detail)


async def _guarded(name: str, factory: Any) -> dict:
    """带硬超时与异常兜底的依赖探测：任何异常都必须变成 JSON，不得漏成 500。"""
    try:
        result = await asyncio.wait_for(factory(), timeout=_HEALTH_PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return _check("unhealthy",
                      "timeout after %ss" % _HEALTH_PROBE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - 白名单会漏 TypeError/自定义异常
        logger.warning("health.probe_failed check={} error={}", name,
                       type(e).__name__)
        return _check("unhealthy", "%s: %s" % (type(e).__name__, str(e)[:120]))
    if not isinstance(result, dict):
        return _check("unhealthy", "probe returned %s" % type(result).__name__)
    if "state" not in result:
        result = dict(result)
        result["state"] = "ok" if result.get("ok") else "unhealthy"
        result.setdefault("ok", result["state"] == "ok")
    return result


@probe_router.get("/health")
@probe_router.get("/healthz", include_in_schema=False)
async def health_probe(request: Request) -> JSONResponse:
    """真实健康探针：三态（ok / degraded / unhealthy），响应体恒为 JSON。"""
    started = time.perf_counter()
    core = getattr(request.app.state, "core", None)
    database, embedding = await asyncio.gather(
        _guarded("database", lambda: _probe_database(core)),
        _guarded("embedding", lambda: asyncio.to_thread(_probe_embedding, core)),
    )
    checks = {"database": database, "embedding": embedding}
    states = [check.get("state") for check in checks.values()]
    if "unhealthy" in states:
        status, status_code = "unhealthy", 503
    elif "degraded" in states:
        status, status_code = "degraded", 200
    else:
        status, status_code = "ok", 200
    payload = {
        "status": status,
        "healthy": status == "ok",
        "reasons": [
            "%s: %s" % (name, check.get("reason"))
            for name, check in checks.items() if check.get("reason")
        ],
        "checks": checks,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )
