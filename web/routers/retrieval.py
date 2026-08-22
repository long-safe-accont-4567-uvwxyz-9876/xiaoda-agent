"""web/routers/retrieval.py — 检索配置与召回测试 REST API.

提供：
- GET  /retrieval/config          — 读取当前检索相关配置（从 config_constants 实时读取）
- PUT  /retrieval/config          — 修改检索配置（写入 webui_overrides.json，热生效）
- POST /retrieval/test            — 用指定查询测试召回率，返回命中结果和分数
- POST /retrieval/config/reset    — 一键恢复默认值
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["retrieval"], dependencies=[Depends(get_current_user)])

_RETRIEVAL_BOOL_KEYS = [
    "RERANKER_ENABLED",
    "QUERY_TRANSFORM_ENABLED",
    "HYDE_ENABLED",
    "MEMORY_RETRIEVAL_DIFFUSION",
    "RETRIEVAL_SMART_SKIP",
    "RETRIEVAL_PARALLEL_TRANSFORM",
    "RETRIEVAL_PARALLEL_SEARCH",
    "QUERY_CACHE_ENABLED",
    "PARENT_CHILD_CHUNK_ENABLED",
    "KG_V2_ENABLED",
    "CONTEXTUAL_RETRIEVAL_ENABLED",
    "MEMORY_DISTILL_ENABLED",
]

_RETRIEVAL_FLOAT_KEYS = [
    ("RAG_RERANK_WEIGHT", 0.60),
    ("RAG_KG_WEIGHT", 0.10),
    ("RAG_IMPORTANCE_WEIGHT", 0.10),
    ("RAG_MIN_FINAL_SCORE", 0.08),
    ("RAG_VEC_MAX_DISTANCE", 1.15),
    ("RAG_VEC_SOFT_PENALTY", 0.6),
    ("QUERY_CACHE_THRESHOLD", 0.88),
    ("MEMORY_WARM_VEC_WEIGHT", 0.6),
    ("EMOTION_TRIGGER_THRESHOLD", 0.5),
]

_RETRIEVAL_INT_KEYS = [
    ("RAG_RECALL_LIMIT", 120),
    ("RAG_RERANK_LIMIT", 60),
    ("QUERY_EXPAND_COUNT", 0),
    ("RERANKER_OVERSAMPLE_RATIO", 3),
    ("QUERY_CACHE_MAX_SIZE", 256),
    ("QUERY_CACHE_TTL", 300),
    ("MEMORY_WARM_MAX", 10),
    ("MEMORY_COLD_MAX", 0),
    ("MEMORY_DISTILL_BATCH", 30),
]

_RETRIEVAL_DEFAULTS: dict[str, Any] = {}
for _k in _RETRIEVAL_BOOL_KEYS:
    _RETRIEVAL_DEFAULTS[_k] = os.getenv(_k, "").lower() not in ("0", "false", "no") if os.getenv(_k) else (
        True if _k in (
            "RERANKER_ENABLED", "QUERY_TRANSFORM_ENABLED",
            "MEMORY_RETRIEVAL_DIFFUSION", "RETRIEVAL_SMART_SKIP",
            "RETRIEVAL_PARALLEL_TRANSFORM", "RETRIEVAL_PARALLEL_SEARCH",
            "QUERY_CACHE_ENABLED", "PARENT_CHILD_CHUNK_ENABLED",
            "CONTEXTUAL_RETRIEVAL_ENABLED",
        ) else False
    )
for _k, _dv in _RETRIEVAL_FLOAT_KEYS:
    _RETRIEVAL_DEFAULTS[_k] = _dv
for _k, _dv in _RETRIEVAL_INT_KEYS:
    _RETRIEVAL_DEFAULTS[_k] = _dv


def _get_config_module():
    import config_constants as cc
    return cc


def _read_current_config() -> dict[str, Any]:
    cc = _get_config_module()
    result: dict[str, Any] = {}
    for k in _RETRIEVAL_BOOL_KEYS:
        result[k] = getattr(cc, k, False)
    for k, _ in _RETRIEVAL_FLOAT_KEYS:
        result[k] = getattr(cc, k, 0.0)
    for k, _ in _RETRIEVAL_INT_KEYS:
        result[k] = getattr(cc, k, 0)
    result["_defaults"] = dict(_RETRIEVAL_DEFAULTS)
    return result


@router.get("/retrieval/config", response_model=Envelope[dict])
async def get_retrieval_config() -> Any:
    return Envelope(data=_read_current_config())


@router.put("/retrieval/config", response_model=Envelope[dict])
async def update_retrieval_config(body: dict, request: Request) -> Any:
    updates = body.get("updates", {})
    if not updates:
        raise HTTPException(400, "updates 不能为空")
    allowed_keys = set(_RETRIEVAL_DEFAULTS.keys())
    invalid = set(updates.keys()) - allowed_keys
    if invalid:
        raise HTTPException(400, f"不允许修改的配置项: {', '.join(invalid)}")
    cc = _get_config_module()
    changed: dict[str, Any] = {}
    for key, value in updates.items():
        if key in _RETRIEVAL_BOOL_KEYS:
            if not isinstance(value, bool):
                raise HTTPException(400, f"{key} 必须是布尔值")
            setattr(cc, key, value)
            os.environ[key] = "true" if value else "false"
            changed[key] = value
        elif key in {k for k, _ in _RETRIEVAL_FLOAT_KEYS}:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} 必须是数值")
            setattr(cc, key, value)
            os.environ[key] = str(value)
            changed[key] = value
        elif key in {k for k, _ in _RETRIEVAL_INT_KEYS}:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} 必须是整数")
            setattr(cc, key, value)
            os.environ[key] = str(value)
            changed[key] = value
    if changed:
        logger.info("retrieval.config_updated keys={}", ",".join(changed.keys()))
    return Envelope(data={"updated": changed, "current": _read_current_config()})


@router.post("/retrieval/config/reset", response_model=Envelope[dict])
async def reset_retrieval_config() -> Any:
    cc = _get_config_module()
    reset_keys: list[str] = []
    for key, default_val in _RETRIEVAL_DEFAULTS.items():
        current = getattr(cc, key, None)
        if current != default_val:
            setattr(cc, key, default_val)
            if key in os.environ:
                del os.environ[key]
            reset_keys.append(key)
    if reset_keys:
        logger.info("retrieval.config_reset keys={}", ",".join(reset_keys))
    return Envelope(data={"reset_keys": reset_keys, "current": _read_current_config()})


@router.post("/retrieval/test", response_model=Envelope[dict])
async def test_retrieval(body: dict, request: Request) -> Any:
    query = body.get("query", "").strip()
    top_k = body.get("top_k", 5)
    if not query:
        raise HTTPException(400, "query 不能为空")
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    core = request.app.state.core
    try:
        results = await core.memory.retrieve_memories_hybrid(
            query=query, k=top_k, use_kg=True
        )
    except Exception as e:
        logger.warning("retrieval.test_failed query={} error={}", query[:50], str(e))
        return Envelope(data={"query": query, "results": [], "error": str(e), "count": 0})
    items = []
    for r in results:
        items.append({
            "id": getattr(r, "id", None),
            "summary": getattr(r, "summary", ""),
            "score": getattr(r, "score", 0),
            "importance": getattr(r, "importance", 0),
            "emotion_label": getattr(r, "emotion_label", ""),
            "source": getattr(r, "source", ""),
        })
    return Envelope(data={"query": query, "results": items, "count": len(items)})