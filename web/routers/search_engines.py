"""web/routers/search_engines.py — 搜索引擎配置与对比测试 REST API.

提供：
- GET  /search-engines/config — 主引擎（SEARCH_ENGINE_PRIMARY）+ 各引擎 Key 配置状态（脱敏）
- PUT  /search-engines/config — 设主引擎 / 保存 Key（与 setup 向导同一 .env 与热加载机制）
- POST /search-engines/test    — 手动输入查询实测：单引擎或三引擎对比（延迟/条数/结果样例）

引擎语义与 tools/web_tools_v2._do_search 一致：primary 仅调整引擎优先序，
不可用的引擎自动跳过，任何失败都回退到剩余引擎（永不因配置而搜不了）。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["search-engines"], dependencies=[Depends(get_current_user)])

_PRIMARY_ENV = "SEARCH_ENGINE_PRIMARY"
_VALID_PRIMARY = {"", "anysearch", "tavily", "bing"}

# 引擎静态元数据（available/Key 状态运行时计算）
_ENGINE_META = [
    {
        "id": "anysearch", "name": "AnySearch 统一搜索",
        "desc": "意图识别+分层路由+跨源融合；需 API Key（或 ANYSEARCH_ENABLED 匿名）",
        "key_env": "ANYSEARCH_API_KEY", "latency_hint": "~1s",
    },
    {
        "id": "tavily", "name": "Tavily AI 搜索",
        "desc": "质量高、带 AI 综合摘要；需 API Key；时效词自动走新闻通道",
        "key_env": "TAVILY_API_KEY", "latency_hint": "~3s",
    },
    {
        "id": "bing", "name": "Bing 抓取",
        "desc": "免费兜底、无需 Key；中文专有名词易跑偏（已知问题）",
        "key_env": "", "latency_hint": "~0.5s",
    },
]

_SEARCH_KEY_ENVS = {"ANYSEARCH_API_KEY", "TAVILY_API_KEY"}


def _engine_available(engine_id: str) -> bool:
    if engine_id == "anysearch":
        from tools.anysearch_client import anysearch_available
        return anysearch_available()
    if engine_id == "tavily":
        from tools.web_tools_v2 import _tavily_available
        return _tavily_available()
    return True  # bing 免费兜底恒可用


def _masked(key_env: str) -> str:
    from web.routers.setup import _mask_key_value
    return _mask_key_value(os.getenv(key_env, ""))


@router.get("/search-engines/config", response_model=Envelope[dict])
async def get_search_engine_config() -> Any:
    engines = []
    for meta in _ENGINE_META:
        key_env = meta["key_env"]
        engines.append({
            **meta,
            "available": _engine_available(meta["id"]),
            "key_configured": bool(key_env and os.getenv(key_env, "").strip()),
            "masked_key": _masked(key_env) if key_env else "",
        })
    return Envelope(data={"primary": os.getenv(_PRIMARY_ENV, "").strip().lower(),
                          "engines": engines})


def _persist_env(updates: dict[str, str]) -> list[str]:
    """写 .env 并热加载（与 setup 向导 save_keys 同一机制：setup_wizard._write_env）。"""
    from setup_wizard import ENV_EXAMPLE_PATH, ENV_PATH, _load_env_values, _parse_env_lines, _write_env
    from web.routers.setup import _write_env_file
    _write_env_file(updates, ENV_PATH, ENV_EXAMPLE_PATH,
                    _parse_env_lines, _load_env_values, _write_env)
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    for k, v in updates.items():
        os.environ[k] = v
    return list(updates.keys())


@router.put("/search-engines/config", response_model=Envelope[dict])
async def update_search_engine_config(body: dict) -> Any:
    primary = str(body.get("primary", "")).strip().lower()
    if primary not in _VALID_PRIMARY:
        raise HTTPException(400, f"primary 必须是 {'/'.join(sorted(_VALID_PRIMARY))} 之一")

    keys = body.get("keys") or {}
    if not isinstance(keys, dict):
        raise HTTPException(400, "keys 必须是对象")
    invalid = set(keys.keys()) - _SEARCH_KEY_ENVS
    if invalid:
        raise HTTPException(400, f"不支持的 Key: {', '.join(invalid)}")
    cleaned = {k: str(v).strip() for k, v in keys.items()}

    updates: dict[str, str] = {}
    if primary != os.getenv(_PRIMARY_ENV, "").strip().lower():
        updates[_PRIMARY_ENV] = primary
    updates.update(cleaned)

    if updates:
        _persist_env(updates)
        logger.info("search_engines.config_updated keys={}", ",".join(updates.keys()))
    return Envelope(data={"saved": list(updates.keys()),
                          "primary": os.getenv(_PRIMARY_ENV, "").strip().lower()})


def _run_engine(engine_id: str, query: str, top_k: int) -> dict[str, Any]:
    """同步执行单引擎搜索（在线程池中运行），返回测试视图。"""
    t0 = time.perf_counter()
    try:
        if engine_id == "anysearch":
            from tools.anysearch_client import anysearch_search_sync
            results, _answer = anysearch_search_sync(query, top_k)
        elif engine_id == "tavily":
            from tools.web_tools_v2 import _tavily_search_sync
            results, _answer = _tavily_search_sync(query, top_k)
        else:
            from tools.web_tools_v2 import _bing_search_sync
            results = _bing_search_sync(query, top_k)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "engine": engine_id, "latency_ms": latency_ms, "count": len(results),
            "results": [{"title": r.get("title", ""),
                         "url": r.get("url", ""),
                         "snippet": (r.get("snippet") or r.get("content") or "")[:200]}
                        for r in results[:top_k]],
        }
    except Exception as e:  # noqa: BLE001 —— 对比测试把单引擎失败作为数据返回
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning("search_engines.test_failed engine={} error={}",
                       engine_id, str(e)[:150])
        return {"engine": engine_id, "latency_ms": latency_ms, "count": 0,
                "results": [], "error": str(e)[:200]}


@router.post("/search-engines/test", response_model=Envelope[dict])
async def test_search_engine(body: dict) -> Any:
    query = str(body.get("query", "")).strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    engine = str(body.get("engine", "")).strip().lower() or "compare"
    if engine not in {"anysearch", "tavily", "bing", "compare"}:
        raise HTTPException(400, "engine 必须是 anysearch/tavily/bing/compare 之一")
    try:
        top_k = max(1, min(int(body.get("top_k", 5)), 10))
    except (TypeError, ValueError):
        top_k = 5

    if engine == "compare":
        runs = await asyncio.gather(*[
            asyncio.to_thread(_run_engine, e, query, top_k)
            for e in ("anysearch", "tavily", "bing")])
        return Envelope(data={"query": query, "mode": "compare", "engines": list(runs)})

    if not _engine_available(engine):
        return Envelope(ok=False, error={
            "code": "ENGINE_UNAVAILABLE",
            "message": f"引擎 {engine} 当前不可用（缺 Key 或被熔断），请先配置"})
    run = await asyncio.to_thread(_run_engine, engine, query, top_k)
    return Envelope(data={"query": query, "mode": "single", **run})
