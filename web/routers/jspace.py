"""J-Space 路由：行为信号流、方向向量、干预闭环、意图分解的实时状态与配置。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["jspace"], dependencies=[Depends(get_current_user)])

_decomposer: Any = None


def _get_decomposer(use_llm: bool = True) -> Any:
    """获取或创建 IntentDecomposer 单例。"""
    global _decomposer
    from core.intent_decomposition import IntentDecomposer
    if _decomposer is None or _decomposer.use_llm != use_llm:
        _decomposer = IntentDecomposer(use_llm_decomposition=use_llm)
    return _decomposer


@router.get("/jspace/status", response_model=Envelope[dict])
async def jspace_status(request: Request) -> Any:
    """J-Space 全局状态概览：各组件是否初始化、信号数量、方向数量、干预规则数量。"""
    status: dict[str, Any] = {
        "enabled": False,
        "signal_stream": {"active": False, "buffer_size": 0},
        "direction_registry": {"active": False, "directions": []},
        "intervention_loop": {"active": False, "rules_count": 0},
        "structured_blackboard": {"active": False},
        "enhanced_router": {"active": False},
        "intent_decomposer": {"active": False, "use_llm": True},
    }
    try:
        from config import ENABLE_J_SPACE_HOOKS
        status["enabled"] = ENABLE_J_SPACE_HOOKS
    except Exception as exc:  # 配置缺失时保持默认 False，仅记录不阻断状态接口
        logger.debug("jspace.status_config_skipped: {}", str(exc)[:120])

    try:
        from core.j_space_bootstrap import (
            get_signal_stream, get_direction_registry,
            get_intervention_loop, get_structured_blackboard,
            get_enhanced_router,
        )
        ss = get_signal_stream()
        if ss is not None:
            status["signal_stream"] = {"active": True, "buffer_size": ss.buffer_size}

        dr = get_direction_registry()
        if dr is not None:
            status["direction_registry"] = {"active": True, "directions": dr.list_directions()}

        il = get_intervention_loop()
        if il is not None:
            status["intervention_loop"] = {"active": True, "rules_count": il.rules_count}

        sb = get_structured_blackboard()
        if sb is not None:
            status["structured_blackboard"] = {"active": True}

        er = get_enhanced_router()
        if er is not None:
            status["enhanced_router"] = {"active": True}
    except Exception as e:
        logger.debug("jspace.status_partial: {}", e)

    return Envelope(data=status)


@router.get("/jspace/signals", response_model=Envelope[dict])
async def jspace_signals(
    request: Request,
    signal_type: str = "",
    last_n: int = 50,
) -> Any:
    """行为信号流历史查询。"""
    try:
        from core.j_space_bootstrap import get_signal_stream
        ss = get_signal_stream()
        if ss is None:
            return Envelope(data={"entries": [], "total": 0})
        entries = ss.get_history(signal_type=signal_type, last_n=last_n)
        return Envelope(data={
            "entries": [
                {
                    "signal_type": e.signal_type,
                    "value": e.value,
                    "source": e.source,
                    "timestamp": e.timestamp,
                    "meta": e.meta,
                }
                for e in entries
            ],
            "total": len(entries),
        })
    except Exception as e:
        logger.debug("jspace.signals_failed: {}", e)
        return Envelope(data={"entries": [], "total": 0})


@router.get("/jspace/signals/aggregate", response_model=Envelope[dict])
async def jspace_signals_aggregate(
    request: Request,
    signal_type: str = "",
    strategy: str = "mean_of_means",
) -> Any:
    """行为信号聚合值。"""
    try:
        from core.j_space_bootstrap import get_signal_stream
        ss = get_signal_stream()
        if ss is None:
            return Envelope(data={"value": 0.0, "signal_type": signal_type, "strategy": strategy})
        value = ss.aggregate(signal_type=signal_type, strategy=strategy)
        return Envelope(data={"value": value, "signal_type": signal_type, "strategy": strategy})
    except Exception as e:
        logger.debug("jspace.aggregate_failed: {}", e)
        return Envelope(data={"value": 0.0, "signal_type": signal_type, "strategy": strategy})


@router.get("/jspace/directions", response_model=Envelope[dict])
async def jspace_directions(request: Request) -> Any:
    """方向向量注册表。"""
    try:
        from core.j_space_bootstrap import get_direction_registry
        dr = get_direction_registry()
        if dr is None:
            return Envelope(data={"directions": {}})
        result = {}
        for name in dr.list_directions():
            d = dr.get(name)
            if d:
                result[name] = {
                    "dimensions": d.dimensions,
                    "source": d.source,
                    "magnitude": d.magnitude,
                }
        return Envelope(data={"directions": result})
    except Exception as e:
        logger.debug("jspace.directions_failed: {}", e)
        return Envelope(data={"directions": {}})


@router.get("/jspace/interventions", response_model=Envelope[dict])
async def jspace_interventions(request: Request) -> Any:
    """干预闭环状态：规则列表 + 收敛指标 + 最近干预历史。"""
    try:
        from core.j_space_bootstrap import get_intervention_loop
        il = get_intervention_loop()
        if il is None:
            return Envelope(data={"rules": [], "convergence": {}, "history": []})
        rules = il.list_rules()
        convergence = il.get_convergence_metrics()
        history = il.recent_interventions
        return Envelope(data={"rules": rules, "convergence": convergence, "history": history})
    except Exception as e:
        logger.debug("jspace.interventions_failed: {}", e)
        return Envelope(data={"rules": [], "convergence": {}, "history": []})


@router.post("/jspace/decompose", response_model=Envelope[dict])
async def jspace_decompose(request: Request, body: dict) -> Any:
    """对给定文本做意图分解（LLM 或规则）。"""
    text = str((body or {}).get("text", ""))
    use_llm = (body or {}).get("use_llm", True)
    if not text:
        return Envelope(data={"factors": [], "residual": 1.0, "dominant": None, "sparsity": 0.0})
    try:
        decomposer = _get_decomposer(use_llm)
        result = await decomposer.encode(text)
        return Envelope(data={
            "factors": [
                {"name": f.name, "activation": f.activation,
                 "evidence": f.evidence, "confidence": f.confidence}
                for f in result.factors
            ],
            "residual": result.residual,
            "dominant": result.dominant_intent.name if result.dominant_intent else None,
            "sparsity": result.sparsity,
        })
    except Exception as e:
        logger.debug("jspace.decompose_failed: {}", e)
        return Envelope(data={"factors": [], "residual": 1.0, "dominant": None, "sparsity": 0.0})


@router.get("/jspace/config", response_model=Envelope[dict])
async def jspace_config_get(request: Request) -> Any:
    """读取 J-Space 配置项。"""
    try:
        from web.config_service import get_config_service
        cfg = get_config_service()
        return Envelope(data={
            "enabled": cfg.get("jspace.enabled", True),
            "signal_max_history": cfg.get("jspace.signal_max_history", 1000),
            "intent_use_llm": cfg.get("jspace.intent_use_llm", True),
            "intent_llm_timeout": cfg.get("jspace.intent_llm_timeout", 10.0),
        })
    except Exception as e:
        logger.debug("jspace.config_get_failed: {}", e)
        return Envelope(data={
            "enabled": True, "signal_max_history": 1000,
            "intent_use_llm": True, "intent_llm_timeout": 10.0,
        })


@router.put("/jspace/config", response_model=Envelope[dict])
async def jspace_config_set(request: Request, body: dict) -> Any:
    """更新 J-Space 配置项。"""
    try:
        from web.config_service import get_config_service
        cfg = get_config_service()
        updates = {}
        if "enabled" in body:
            updates["jspace.enabled"] = bool(body["enabled"])
        if "signal_max_history" in body:
            updates["jspace.signal_max_history"] = int(body["signal_max_history"])
        if "intent_use_llm" in body:
            updates["jspace.intent_use_llm"] = bool(body["intent_use_llm"])
        if "intent_llm_timeout" in body:
            updates["jspace.intent_llm_timeout"] = float(body["intent_llm_timeout"])
        if updates:
            cfg.set_many(updates)
        return Envelope(data={"updated": list(updates.keys())})
    except Exception as e:
        logger.debug("jspace.config_set_failed: {}", e)
        return Envelope(data={"updated": []})