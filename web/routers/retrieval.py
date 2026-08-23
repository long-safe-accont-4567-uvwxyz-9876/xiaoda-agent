"""web/routers/retrieval.py — 检索配置与召回测试 REST API.

提供：
- GET  /retrieval/config          — 读取当前检索相关配置（从 config_constants 实时读取）
- PUT  /retrieval/config          — 修改检索配置（写入 webui_overrides.json，热生效）
- POST /retrieval/test            — 单查询召回测试；带期望基准(expect_keywords/expect_ids)
                                    时返回 recall/precision/F1/MRR 等指标，另有耗时/分数分布
- POST /retrieval/evaluate        — 批量评测：多变例宏平均指标 + P95 延迟 + 失败计数
- POST /retrieval/config/reset    — 一键恢复默认值

指标口径（期望项 = 关键词 + ID 的并集）：
- recall    = 被任一返回结果覆盖的期望项 / 期望项总数
- precision = 命中期望的结果数 / 返回结果数
- f1        = 二者调和平均；mrr = 1/首个命中结果的名次
"""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["retrieval"], dependencies=[Depends(get_current_user)])

_EVAL_MAX_CASES = 50

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
    query = str(body.get("query", "")).strip()
    top_k = body.get("top_k", 5)
    if not query:
        raise HTTPException(400, "query 不能为空")
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    expect_keywords, expect_ids = _parse_expectations(body)
    core = request.app.state.core
    t0 = time.perf_counter()
    try:
        results = await core.memory.retrieve_memories_hybrid(
            query=query, k=top_k, use_kg=True
        )
    except Exception as e:  # noqa: BLE001 —— 测试端点把失败作为数据返回
        logger.warning("retrieval.test_failed query={} error={}", query[:50], str(e))
        return Envelope(data={"query": query, "results": [], "error": str(e), "count": 0})
    latency_ms = (time.perf_counter() - t0) * 1000
    items = _build_items(results)
    metrics = _annotate_and_measure(items, expect_keywords, expect_ids, latency_ms)
    return Envelope(data={"query": query, "results": items, "count": len(items),
                          "metrics": metrics})


@router.post("/retrieval/evaluate", response_model=Envelope[dict])
async def evaluate_retrieval(body: dict, request: Request) -> Any:
    cases = body.get("cases")
    if not isinstance(cases, list) or not cases:
        raise HTTPException(400, "cases 不能为空（每项 {query, expect_keywords?, expect_ids?}）")
    if len(cases) > _EVAL_MAX_CASES:
        raise HTTPException(400, f"评测用例一次最多 {_EVAL_MAX_CASES} 条")
    top_k = body.get("top_k", 5)
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    core = request.app.state.core
    per_case: list[dict[str, Any]] = []
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            raise HTTPException(400, f"cases[{idx}] 必须是对象")
        query = str(case.get("query", "")).strip()
        if not query:
            raise HTTPException(400, f"cases[{idx}].query 不能为空")
        try:
            expect_keywords, expect_ids = _parse_expectations(case)
        except HTTPException as e:
            raise HTTPException(400, f"cases[{idx}]: {e.detail}") from e
        t0 = time.perf_counter()
        try:
            results = await core.memory.retrieve_memories_hybrid(
                query=query, k=top_k, use_kg=True
            )
        except Exception as e:  # noqa: BLE001 —— 单用例失败不中断整批评测
            logger.warning("retrieval.evaluate_case_failed query={} error={}",
                           query[:50], str(e))
            per_case.append({"query": query, "results": [], "count": 0,
                             "error": str(e), "metrics": None})
            continue
        latency_ms = (time.perf_counter() - t0) * 1000
        items = _build_items(results)
        metrics = _annotate_and_measure(items, expect_keywords, expect_ids, latency_ms)
        per_case.append({"query": query, "results": items, "count": len(items),
                         "metrics": metrics})
    return Envelope(data={"top_k": top_k, "cases": per_case, **_aggregate(per_case)})


def _parse_expectations(body: dict) -> tuple[list[str], list[str]]:
    expect_keywords = body.get("expect_keywords") or []
    expect_ids = body.get("expect_ids") or []
    if not isinstance(expect_keywords, list) or not all(isinstance(k, str) for k in expect_keywords):
        raise HTTPException(400, "expect_keywords 必须是字符串数组")
    if not isinstance(expect_ids, list) or not all(
            isinstance(i, (str, int)) for i in expect_ids):
        raise HTTPException(400, "expect_ids 必须是字符串/整数数组")
    kws = [k.strip() for k in expect_keywords if k.strip()]
    ids = [str(i).strip() for i in expect_ids if str(i).strip()]
    return kws, ids


def _build_items(results: list[Any]) -> list[dict[str, Any]]:
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
    return items


def _result_matches(item: dict[str, Any], kws: list[str], ids: list[str]) -> list[str]:
    """该结果命中的期望项：关键词（summary 子串，不区分大小写）+ ID。"""
    hit: list[str] = []
    summary = str(item.get("summary") or "").lower()
    rid = item.get("id")
    for kw in kws:
        if kw.lower() in summary:
            hit.append(kw)
    for eid in ids:
        if rid is not None and str(rid) == eid:
            hit.append(f"id:{eid}")
    return hit


def _annotate_and_measure(items: list[dict[str, Any]], kws: list[str], ids: list[str],
                          latency_ms: float) -> dict[str, Any]:
    """就地给每条结果标 matched/matched_keywords，并汇总单查询指标。"""
    covered: set[str] = set()
    matched_count = 0
    first_hit_rank = 0
    for rank, it in enumerate(items, start=1):
        hit = _result_matches(it, kws, ids)
        it["matched"] = bool(hit)
        it["matched_keywords"] = hit
        if hit:
            matched_count += 1
            if not first_hit_rank:
                first_hit_rank = rank
            covered.update(hit)
    scores = [float(it.get("score") or 0) for it in items]
    threshold = float(getattr(_get_config_module(), "RAG_MIN_FINAL_SCORE", 0.08))
    metrics: dict[str, Any] = {
        "latency_ms": round(latency_ms, 1),
        "returned": len(items),
        "score_max": round(max(scores), 4) if scores else 0.0,
        "score_min": round(min(scores), 4) if scores else 0.0,
        "score_mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "threshold": threshold,
        "above_threshold": sum(1 for s in scores if s >= threshold),
        "has_expect": bool(kws or ids),
    }
    if metrics["has_expect"]:
        total_expect = len(kws) + len(ids)
        recall = len(covered) / total_expect if total_expect else 0.0
        precision = matched_count / len(items) if items else 0.0
        metrics.update({
            "expect_total": total_expect,
            "expect_covered": len(covered),
            "matched_results": matched_count,
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4)
                  if (precision + recall) else 0.0,
            "first_hit_rank": first_hit_rank,
            "mrr": round(1.0 / first_hit_rank, 4) if first_hit_rank else 0.0,
            "hit": matched_count > 0,
        })
    return metrics


def _aggregate(per_case: list[dict[str, Any]]) -> dict[str, Any]:
    ok_cases = [c for c in per_case if c.get("metrics")]
    with_expect = [c for c in ok_cases if c["metrics"].get("has_expect")]

    def _macro(field: str) -> float:
        vals = [c["metrics"][field] for c in with_expect]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    latencies = sorted(c["metrics"]["latency_ms"] for c in ok_cases)
    p95_idx = max(0, -(-len(latencies) * 95 // 100) - 1)  # 向上取整的名次法
    return {
        "cases_total": len(per_case),
        "cases_ok": len(ok_cases),
        "cases_failed": len(per_case) - len(ok_cases),
        "cases_with_expect": len(with_expect),
        "aggregate": {
            "recall_macro": _macro("recall"),
            "precision_macro": _macro("precision"),
            "f1_macro": _macro("f1"),
            "mrr_macro": _macro("mrr"),
            "hit_rate": round(
                sum(1 for c in with_expect if c["metrics"]["hit"]) / len(with_expect), 4
            ) if with_expect else 0.0,
            "latency_avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "latency_p95_ms": latencies[p95_idx] if latencies else 0.0,
        },
    }
