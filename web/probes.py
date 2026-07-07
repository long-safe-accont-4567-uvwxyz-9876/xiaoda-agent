"""健康探针（R12）— LLM / TTS / 视频 / MCP / DB / 向量库 在线探活。"""
from __future__ import annotations
from typing import Any

import json
import time
import asyncio
from pathlib import Path

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
            (getattr(getattr(result, "choices", [None])[0], "message", None) and
             result.choices[0].message.content or "")
        ok = bool(text and text.strip())
        return {"ok": ok, "latency_ms": int((time.time() - t0) * 1000),
                "model": ROUTE_TABLE[route].get("model", ""),
                "reply_excerpt": (text or "")[:60],
                "error": "" if ok else "空回复"}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "model": ROUTE_TABLE[route].get("model", ""), "error": str(e)[:200]}


async def probe_provider(core: Any, provider_id: str) -> dict:
    """对自定义 provider 直连探活（不经过 ROUTE_TABLE）。

    与 probe_llm 不同，此函数绕过 ModelRouter.route()，直接用 provider
    配置 + Key 构建临时客户端，避免依赖路由表注册。
    """
    import asyncio
    from web.config_service import get_config_service
    from web.routers.models import load_provider_key
    from web.custom_providers import build_client

    # 内置 provider 走标准路由探针
    if provider_id in ("mimo", "agnes"):
        route = "chat" if provider_id == "mimo" else "chat_agnes"
        return await probe_llm(core, route)

    cfg = get_config_service()
    t0 = time.time()
    record = cfg.get(f"models.providers.{provider_id}")
    if not record:
        return {"ok": False, "error": f"provider {provider_id} 不存在", "latency_ms": 0}
    key = load_provider_key(provider_id)
    if not key:
        return {"ok": False, "error": "未配置 API Key", "latency_ms": 0}

    model = await _resolve_provider_model(record, provider_id, key)
    if not model:
        return {"ok": False, "error": "未配置 default_model，请在该 provider 设置中填写默认模型名称", "latency_ms": 0}

    return await _perform_provider_probe(record, key, model, t0)


async def _resolve_provider_model(record: dict, provider_id: str, key: str) -> str:
    """解析 provider 的默认模型，依次从 record/ROUTE_TABLE/API 列表获取"""
    import asyncio
    from web.custom_providers import build_client

    model = record.get("default_model") or ""
    if model:
        return model

    # 从路由表中查找该 provider 对应的模型作为 fallback
    from model_router import ROUTE_TABLE
    for _route_cfg in ROUTE_TABLE.values():
        if _route_cfg.get("client") == provider_id and _route_cfg.get("model"):
            return _route_cfg["model"]

    # 尝试通过 API 列出可用模型，优先选择免费/轻量模型
    try:
        client = build_client(record.get("format", "openai"), record["base_url"], key)
        models_resp = await asyncio.wait_for(client.models.list(), timeout=10)
        model_list = models_resp.data if hasattr(models_resp, "data") else []
        if model_list:
            return _pick_model_from_list(model_list)
    except Exception:
        logger.debug("probes.model_list_error", exc_info=True)
    return ""


def _pick_model_from_list(model_list: list) -> str:
    """从 API 返回的模型列表中选择免费/轻量对话模型"""
    # 优先选择免费对话模型（排除 code/content-safety 等非对话模型）
    _free_chat = [m for m in model_list
                  if hasattr(m, "id") and ":free" in m.id
                  and not any(kw in m.id.lower()
                  for kw in ("code", "content-safety", "nano-omni", "vl"))]
    _light = [m for m in model_list
              if hasattr(m, "id") and any(kw in m.id.lower()
              for kw in ("qwen2.5-7b", "qwen2-7b", "gpt-3.5", "llama-3", "deepseek-chat"))]
    _pick = (_free_chat or _light or model_list)[0]
    return _pick.id if hasattr(_pick, "id") else str(_pick)


async def _perform_provider_probe(record: dict, key: str, model: str, t0: float) -> dict:
    """构建临时客户端并发起探活请求"""
    import asyncio
    from web.custom_providers import build_client

    try:
        client = build_client(record.get("format", "openai"), record["base_url"], key)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "请只回复四个字：草元素已就绪"}],
                max_tokens=30),
            timeout=30)
        text = resp.choices[0].message.content or ""
        return {"ok": bool(text.strip()), "latency_ms": int((time.time() - t0) * 1000),
                "model": model, "reply_excerpt": text[:60],
                "error": "" if text.strip() else "空回复"}
    except Exception as e:
        err_msg = _format_provider_error(str(e)[:200])
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "model": model, "error": err_msg}


def _format_provider_error(err_msg: str) -> str:
    """识别常见错误并给出友好提示"""
    if "402" in err_msg or "Insufficient credits" in err_msg or "never purchased" in err_msg:
        return "账户余额不足，请充值后再试"
    if "403" in err_msg and "region" in err_msg:
        return "该模型在当前地区不可用"
    if "403" in err_msg and "not available" in err_msg:
        return "该模型不可用，请更换 default_model"
    return err_msg


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
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


async def probe_vector(core: Any) -> dict:
    """探测向量记忆库检索能力, 返回命中数."""
    t0 = time.time()
    try:
        if not core.memory:
            return {"ok": False, "latency_ms": 0, "error": "MemoryManager 未初始化"}
        results = await core.memory.retrieve_memories("测试", k=1)
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "hits": len(results)}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


def _list_custom_providers(core: Any) -> list[dict]:
    """枚举已注册的自定义 provider（有 Key 且非内置），返回测试项清单。"""
    out: list[dict] = []
    try:
        from web.config_service import get_config_service
        from web.routers.models import load_provider_key
        cfg = get_config_service()
        custom = cfg.get("models.providers", {}) or {}
        for pid, p in custom.items():
            if pid in ("mimo", "agnes"):
                continue
            key = load_provider_key(pid)
            if not key:
                continue
            if not p.get("enabled", True):
                continue
            label = p.get("label", pid)
            model = p.get("default_model", "")
            out.append({
                "id": f"llm_provider:{pid}",
                "label": f"{label} · {model}" if model else f"{label} · （未设默认模型）",
                "detail": model or pid,
                "provider_id": pid,
                "model_id": model,
            })
    except Exception as e:
        logger.debug("probes.list_custom_providers_failed error={}", str(e))
    return out


def list_probe_ids(core: Any) -> list[dict]:
    """全部可用探针清单（供测试中心渲染卡片）。"""
    from model_router import ROUTE_TABLE
    probes = [{"id": f"llm:{r}", "label": f"LLM · {r}",
               "detail": ROUTE_TABLE[r].get("model", "")} for r in ROUTE_TABLE]
    # 追加已注册的自定义 provider 测试项
    probes.extend(_list_custom_providers(core))
    probes.append({"id": "tts", "label": "TTS 语音合成", "detail": "mimo voiceclone"})
    probes.append({"id": "video", "label": "视频生成配置", "detail": "agnes"})
    try:
        for name in core._mcp_manager._clients:
            probes.append({"id": f"mcp:{name}", "label": f"MCP · {name}", "detail": "stdio"})
    except Exception:
        logger.debug("probes.mcp_clients_error", exc_info=True)
    probes.append({"id": "db", "label": "数据库", "detail": "SQLite"})
    probes.append({"id": "vector", "label": "向量记忆库", "detail": "sqlite-vec"})
    return probes


async def run_probe(core: Any, probe_id: str) -> dict:
    """按 probe_id 执行单个探针, 返回结果字典.

    Args:
        core: 应用核心对象
        probe_id: 探针 ID (如 llm:default/tts/mcp:xxx/db/vector)

    Returns:
        探针结果字典
    """
    if probe_id.startswith("llm_provider:"):
        return await probe_provider(core, probe_id[len("llm_provider:"):])
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


async def run_all(core: Any, on_progress: Any | None=None) -> dict:
    """执行全部探针（并发，最多 5 个同时运行），可选回调通知单条进度.

    Args:
        core: 应用核心对象
        on_progress: 单条探针完成后的回调 (item, res)

    Returns:
        含 total/passed/failed/results 的汇总字典
    """
    items = list_probe_ids(core)
    semaphore = asyncio.Semaphore(5)
    lock = asyncio.Lock()
    results: list = []
    passed = 0

    async def _run_one(item: dict) -> None:
        nonlocal passed
        async with semaphore:
            res = await run_probe(core, item["id"])
            res["id"] = item["id"]
            res["label"] = item["label"]
            async with lock:
                results.append(res)
                if res.get("ok"):
                    passed += 1
            if on_progress:
                try:
                    await on_progress(item["id"], res)
                except Exception:
                    logger.debug("probes.progress_callback_error", exc_info=True)

    await asyncio.gather(*[_run_one(item) for item in items])
    report = {"run_at": time.time(), "passed": passed, "total": len(items), "detail": results}
    try:
        await core.db.execute(
            "INSERT INTO health_reports(run_at, passed, total, detail) VALUES (?,?,?,?)",
            (report["run_at"], passed, len(items), json.dumps(results, ensure_ascii=False)))
    except Exception as e:
        logger.warning("health.report_save_failed error={}", str(e))
    return report
