"""健康探针（R12）— LLM / TTS / 视频 / MCP / DB / 向量库 在线探活。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


async def probe_llm(core: Any, route: str = "chat") -> dict:
    """直连指定路由发送固定探针。"""
    # 注意：不再每次探活都 refresh_client()，避免频繁重建客户端
    # Setup 保存 Key 后会主动调用 refresh_client()
    from model_router import ROUTE_TABLE
    if route not in ROUTE_TABLE:
        return {"ok": False, "error": f"未知路由 {route}", "latency_ms": 0}
    t0 = time.time()
    try:
        result = await core.router.route(
            route,
            [{"role": "user", "content": "请只回复四个字：草元素已就绪"}],
            max_tokens=30, timeout=30)
        text = result if isinstance(result, str) else \
            ((getattr(getattr(result, "choices", [None])[0], "message", None) and
              result.choices[0].message.content) or "")
        ok = bool(text and text.strip())
        return {"ok": ok, "latency_ms": int((time.time() - t0) * 1000),
                "model": ROUTE_TABLE[route].get("model", ""),
                "reply_excerpt": (text or "")[:60],
                "error": "" if ok else "空回复"}
    except (RuntimeError, OSError, ValueError, ConnectionError,
                httpx.TimeoutException, httpx.RequestError) as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "model": ROUTE_TABLE[route].get("model", ""), "error": str(e)[:200]}


async def probe_provider(core: Any, provider_id: str, provider_service: Any | None = None) -> dict:
    t0 = time.time()
    service = provider_service
    if service is None:
        from web.app_ref import get_app
        app = get_app()
        service = getattr(getattr(app, "state", None), "provider_service", None)
        if service is None:
            return {"ok": False, "latency_ms": 0, "error": "provider service 未初始化"}
    try:
        report = await service.capabilities(provider_id)
    except KeyError:
        return {"ok": False, "error": f"provider {provider_id} 不存在", "latency_ms": 0}
    except (RuntimeError, OSError, ValueError, ConnectionError,
                httpx.TimeoutException, httpx.RequestError) as error:
        return {"ok": False, "error": str(error)[:200], "latency_ms": int((time.time() - t0) * 1000)}
    return {
        "ok": report.available,
        "latency_ms": int((time.time() - t0) * 1000),
        "models": list(report.models),
        "error": report.error or "",
    }


async def probe_tts(core: Any) -> dict:
    """探测 TTS 合成能力, 返回结果与音频 URL."""
    t0 = time.time()
    try:
        if not core.tts.available:
            return {"ok": False, "latency_ms": 0, "error": "TTS 引擎不可用（缺 API Key 或参考音频）"}
        path = await core.tts.synthesize("小妲在哦～", voice="xiaoda")
        ok = bool(path and Path(path).exists() and Path(path).stat().st_size > 1024)
        audio_url = None
        if ok:
            import shutil

            from web.media_tasks import MEDIA_ROOT
            dest = MEDIA_ROOT / "tts" / Path(path).name
            if Path(path).resolve() != dest.resolve():
                shutil.copy2(str(path), str(dest))
            audio_url = f"/media/tts/{dest.name}"
        return {"ok": ok, "latency_ms": int((time.time() - t0) * 1000),
                "audio_url": audio_url, "error": "" if ok else "合成产物缺失或过小"}
    except (RuntimeError, OSError, ValueError, ConnectionError,
                httpx.TimeoutException, httpx.RequestError) as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


async def probe_video_config() -> dict:
    """视频生成走异步任务（真实出片耗时长），探针只验证配置与速率门。"""
    t0 = time.time()
    try:
        import os
        key = os.getenv("AGNES_API_KEY", "")
        if not key:
            return {"ok": False, "latency_ms": 0, "error": "AGNES_API_KEY 未配置"}
        from tool_engine.tool_registry import get_tool
        if not get_tool("agnes_video_generate"):
            return {"ok": False, "latency_ms": 0, "error": "视频生成工具未注册"}
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "note": "配置就绪。完整出片测试请在媒体工坊提交任务。"}
    except (RuntimeError, OSError, ValueError, ImportError) as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)[:200]}


async def probe_mcp(core: Any, server: str) -> dict:
    """探测指定 MCP server 连接状态与工具列表.

    Args:
        core: 应用核心对象
        server: MCP server 名

    Returns:
        含 ok/latency_ms/tools/error 的探测结果
    """
    t0 = time.time()
    try:
        client = core._mcp_manager._clients.get(server)
        if not client:
            return {"ok": False, "latency_ms": 0, "error": f"MCP server {server} 未运行"}
        ok = client.available
        return {"ok": ok, "latency_ms": int((time.time() - t0) * 1000),
                "tools": sorted(client.tool_names),
                "error": "" if ok else "连接不可用"}
    except (RuntimeError, OSError, ValueError) as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)[:200]}


async def probe_db(core: Any) -> dict:
    """探测数据库连接, 返回会话/记忆条目计数."""
    t0 = time.time()
    try:
        row = await core.db.fetch_one("SELECT COUNT(*) AS c FROM conversation_logs")
        mem = await core.db.fetch_one("SELECT COUNT(*) AS c FROM episodic_memories")
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "conversations": row["c"] if row else 0,
                "memories": mem["c"] if mem else 0}
    except (OSError, RuntimeError, ValueError) as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


async def probe_vector(core: Any) -> dict:
    """探测向量记忆库检索能力, 返回命中数."""
    t0 = time.time()
    try:
        if not core.memory:
            return {"ok": False, "latency_ms": 0, "error": "MemoryManager 未初始化"}
        # 显式绑定探针专用 scope：API 上下文没有 chat 流的 scope contextvar，
        # 不传会触发 current_scope() RuntimeError("memory request scope is not bound")
        from memory.scope import Scope
        # record_access=False 只读探测：不触发 FSRS/touch 写副作用，
        # 避免探针污染真实用户记忆的生命周期状态
        results = await core.memory.retrieve_memories(
            "测试", k=1, scope=Scope(session_id="health-probe"),
            record_access=False,
        )
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "hits": len(results)}
    except (RuntimeError, OSError, ValueError) as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


def _list_custom_providers(core: Any, provider_service: Any | None = None) -> list[dict]:
    """枚举已注册的自定义 provider（有 Key 且非内置），返回测试项清单。"""
    out: list[dict] = []
    try:
        service = provider_service
        if service is None:
            from web.app_ref import get_app
            app = get_app()
            service = getattr(getattr(app, "state", None), "provider_service", None)
            if service is None:
                return out
        for definition in service.list():
            if definition.builtin:
                continue
            if not definition.metadata.get("enabled", True):
                continue
            pid = definition.id
            label = definition.metadata.get("label", pid)
            model = definition.default_model
            out.append({
                "id": f"llm_provider:{pid}",
                "label": f"{label} · {model}" if model else f"{label} · （未设默认模型）",
                "detail": model or pid,
                "provider_id": pid,
                "model_id": model,
            })
    except (RuntimeError, ImportError, ValueError) as e:
        logger.debug("probes.list_custom_providers_failed error={}", str(e))
    return out


def list_probe_ids(core: Any, provider_service: Any | None = None) -> list[dict]:
    """全部可用探针清单（供测试中心渲染卡片）。"""
    from model_router import ROUTE_TABLE
    probes = [{"id": f"llm:{r}", "label": f"LLM · {r}",
               "detail": ROUTE_TABLE[r].get("model", "")} for r in ROUTE_TABLE]
    # 追加已注册的自定义 provider 测试项
    probes.extend(_list_custom_providers(core, provider_service))
    probes.append({"id": "tts", "label": "TTS 语音合成", "detail": "mimo voiceclone"})
    probes.append({"id": "video", "label": "视频生成配置", "detail": "agnes"})
    try:
        for name in core._mcp_manager._clients:
            probes.append({"id": f"mcp:{name}", "label": f"MCP · {name}", "detail": "stdio"})
    except (RuntimeError, AttributeError):
        logger.debug("probes.mcp_clients_error", exc_info=True)
    probes.append({"id": "db", "label": "数据库", "detail": "SQLite"})
    probes.append({"id": "vector", "label": "向量记忆库", "detail": "sqlite-vec"})
    return probes


async def run_probe(core: Any, probe_id: str, provider_service: Any | None = None) -> dict:
    """按 probe_id 执行单个探针, 返回结果字典.

    Args:
        core: 应用核心对象
        probe_id: 探针 ID (如 llm:default/tts/mcp:xxx/db/vector)

    Returns:
        探针结果字典
    """
    if probe_id.startswith("llm_provider:"):
        return await probe_provider(core, probe_id[len("llm_provider:"):], provider_service)
    if probe_id.startswith("llm:"):
        return await probe_llm(core, probe_id[4:])
    if probe_id == "tts":
        return await probe_tts(core)
    if probe_id == "video":
        return await probe_video_config()
    if probe_id.startswith("mcp:"):
        return await probe_mcp(core, probe_id[4:])
    if probe_id == "db":
        return await probe_db(core)
    if probe_id == "vector":
        return await probe_vector(core)
    return {"ok": False, "error": f"未知探针 {probe_id}"}


async def run_all(core: Any, on_progress: Any | None=None, provider_service: Any | None = None) -> dict:
    """执行全部探针（并发，最多 5 个同时运行），可选回调通知单条进度.

    Args:
        core: 应用核心对象
        on_progress: 单条探针完成后的回调 (item, res)

    Returns:
        含 total/passed/failed/results 的汇总字典
    """
    items = list_probe_ids(core, provider_service)
    semaphore = asyncio.Semaphore(5)
    lock = asyncio.Lock()
    results: list = []
    passed = 0

    async def _run_one(item: dict) -> None:
        nonlocal passed
        async with semaphore:
            # 单探针异常不应阻断整批：捕获后转为失败结果
            # CR-FIX: 捕获实际耗时 + 清理异常文本（防敏感信息泄漏）
            _probe_t0 = time.time()
            try:
                res = await run_probe(core, item["id"], provider_service)
            except (RuntimeError, OSError, ValueError, ConnectionError, TimeoutError) as e:
                _elapsed = int((time.time() - _probe_t0) * 1000)
                # 截断异常文本，防止大响应体/路径/密钥泄漏到 health_reports
                _err_text = str(e)[:300]
                res = {"ok": False, "error": f"探针异常: {_err_text}", "latency_ms": _elapsed}
            res["id"] = item["id"]
            res["label"] = item["label"]
            async with lock:
                results.append(res)
                if res.get("ok"):
                    passed += 1
            if on_progress:
                try:
                    await on_progress(item["id"], res)
                except (RuntimeError, OSError):
                    logger.debug("probes.progress_callback_error", exc_info=True)

    await asyncio.gather(*[_run_one(item) for item in items], return_exceptions=True)
    report = {"run_at": time.time(), "passed": passed, "total": len(items), "detail": results}
    try:
        await core.db.execute(
            "INSERT INTO health_reports(run_at, passed, total, detail) VALUES (?,?,?,?)",
            (report["run_at"], passed, len(items), json.dumps(results, ensure_ascii=False)))
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("health.report_save_failed error={}", str(e))
    return report
