# tests/test_intervention_loop.py

import pytest

from core.behavioral_direction import DirectionRegistry, DirectionVector
from core.behavioral_signal import BehavioralSignalStream
from core.intervention_loop import InterventionLoop, InterventionRule


@pytest.mark.asyncio
async def test_evaluate_below_threshold_no_trigger():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.8, direction_name="calm", alpha=0.5))
    await stream.emit("cognitive_load", 0.5, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 0


@pytest.mark.asyncio
async def test_evaluate_above_threshold_triggers():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.8, direction_name="calm", alpha=0.5))
    await stream.emit("cognitive_load", 0.9, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 1
    assert triggered[0]["direction"] == "calm"
    assert triggered[0]["alpha"] == 0.5


@pytest.mark.asyncio
async def test_cooldown_prevents_retrigger():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.8, direction_name="calm",
                                        alpha=0.5, cooldown=30.0))
    await stream.emit("cognitive_load", 0.9, "test")
    triggered1 = await loop.evaluate({})
    assert len(triggered1) == 1
    # 立即再次评估，cooldown 内不应触发
    await stream.emit("cognitive_load", 0.95, "test")
    triggered2 = await loop.evaluate({})
    assert len(triggered2) == 0


@pytest.mark.asyncio
async def test_apply_intervention_projected():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual"))
    loop = InterventionLoop(stream, registry)
    intervention = {
        "scaled_direction": DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual") * 0.5,
        "mode": "projected",
    }
    context = {}
    result = await loop.apply_intervention(context, intervention)
    assert result["emotion_offset"] == -0.2
    assert result["prompt_modifier"] == 0.1


@pytest.mark.asyncio
async def test_apply_intervention_uniform():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("focused", {"prompt": 0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    intervention = {
        "scaled_direction": DirectionVector("focused", {"prompt": 0.4}, "manual") * 1.0,
        "mode": "uniform",
    }
    context = {}
    result = await loop.apply_intervention(context, intervention)
    assert result["prompt_modifier"] == 0.4


@pytest.mark.asyncio
async def test_missing_direction_skipped():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.5, direction_name="nonexistent", alpha=0.5))
    await stream.emit("cognitive_load", 0.9, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 0


@pytest.mark.asyncio
async def test_convergence_metrics_initial():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    loop = InterventionLoop(stream, registry)
    metrics = loop.get_convergence_metrics()
    assert metrics["converging"] is True
    assert metrics["intervention_count"] == 0


@pytest.mark.asyncio
async def test_convergence_metrics_after_interventions():
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule("cognitive_load", threshold=0.5, direction_name="calm",
                                        alpha=0.5, cooldown=0.0))
    # 触发多次干预
    for score in [0.9, 0.8, 0.7, 0.6, 0.5]:
        await stream.emit("cognitive_load", score, "test")
        await loop.evaluate({})
    metrics = loop.get_convergence_metrics()
    assert metrics["intervention_count"] >= 5
    assert metrics["converging"] is True  # score 在下降
