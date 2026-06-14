"""健康探针（R12）— LLM / TTS / 视频 / MCP / DB / 向量库 在线探活。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger


async def probe_llm(core, route: str = "chat") -> dict:
    """直连指定路由发送固定探针。"""
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


async def probe_tts(core) -> dict:
    t0 = time.time()
    try:
        if not core.tts.available:
            return {"ok": False, "latency_ms": 0, "error": "TTS 引擎不可用（缺 API Key 或参考音频）"}
        path = await core.tts.synthesize("纳西妲在哦～", voice="nahida")
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


async def probe_mcp(core, server: str) -> dict:
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


async def probe_db(core) -> dict:
    t0 = time.time()
    try:
        row = await core.db.fetch_one("SELECT COUNT(*) AS c FROM conversation_logs")
        mem = await core.db.fetch_one("SELECT COUNT(*) AS c FROM episodic_memories")
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "conversations": row["c"] if row else 0,
                "memories": mem["c"] if mem else 0}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


async def probe_vector(core) -> dict:
    t0 = time.time()
    try:
        if not core.memory:
            return {"ok": False, "latency_ms": 0, "error": "MemoryManager 未初始化"}
        results = await core.memory.retrieve_memories("测试", k=1)
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "hits": len(results)}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


def list_probe_ids(core) -> list[dict]:
    """全部可用探针清单（供测试中心渲染卡片）。"""
    from model_router import ROUTE_TABLE
    probes = [{"id": f"llm:{r}", "label": f"LLM · {r}",
               "detail": ROUTE_TABLE[r].get("model", "")} for r in ROUTE_TABLE]
    probes.append({"id": "tts", "label": "TTS 语音合成", "detail": "mimo voiceclone"})
    probes.append({"id": "video", "label": "视频生成配置", "detail": "agnes"})
    try:
        for name in core._mcp_manager._clients:
            probes.append({"id": f"mcp:{name}", "label": f"MCP · {name}", "detail": "stdio"})
    except Exception:
        pass
    probes.append({"id": "db", "label": "数据库", "detail": "SQLite"})
    probes.append({"id": "vector", "label": "向量记忆库", "detail": "sqlite-vec"})
    return probes


async def run_probe(core, probe_id: str) -> dict:
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


async def run_all(core, on_progress=None) -> dict:
    items = list_probe_ids(core)
    results = []
    passed = 0
    for item in items:
        res = await run_probe(core, item["id"])
        res["id"] = item["id"]
        res["label"] = item["label"]
        results.append(res)
        if res.get("ok"):
            passed += 1
        if on_progress:
            try:
                await on_progress(item["id"], res)
            except Exception:
                pass
    report = {"run_at": time.time(), "passed": passed, "total": len(items), "detail": results}
    try:
        await core.db.execute(
            "INSERT INTO health_reports(run_at, passed, total, detail) VALUES (?,?,?,?)",
            (report["run_at"], passed, len(items), json.dumps(results, ensure_ascii=False)))
    except Exception as e:
        logger.warning("health.report_save_failed error={}", str(e))
    return report
