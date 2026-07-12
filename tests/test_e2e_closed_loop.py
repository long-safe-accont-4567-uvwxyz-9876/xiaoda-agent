# tests/test_e2e_closed_loop.py
"""
端到端闭环测试 — 验证完整的 观测→干预→验证 闭环。

流程:
1. emit cognitive_load 信号（高值）
2. InterventionLoop.evaluate() 触发干预
3. apply_intervention 应用方向到上下文
4. 验证 context 被修改
5. 验证 convergence_metrics 追踪到干预历史
"""
import pytest
import asyncio
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.intervention_loop import InterventionRule, InterventionLoop


@pytest.mark.asyncio
async def test_e2e_closed_loop_signal_to_intervention():
    """完整闭环：信号 → 阈值判断 → 方向应用 → 上下文修改"""
    # 1. 初始化所有组件
    stream = BehavioralSignalStream(max_history=100)
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual"))
    loop = InterventionLoop(stream, registry)

    # 2. 注册干预规则
    loop.register_rule(InterventionRule(
        signal_type="cognitive_load",
        threshold=0.8,
        direction_name="calm",
        alpha=0.5,
        mode="projected",
        cooldown=0.0,  # 测试中禁用冷却
    ))

    # 3. 发射高 cognitive_load 信号
    await stream.emit("cognitive_load", 0.9, "test_e2e")

    # 4. 评估触发
    triggered = await loop.evaluate({})
    assert len(triggered) == 1, "应该触发 1 个干预"
    assert triggered[0]["direction"] == "calm"
    assert triggered[0]["score"] == 0.9

    # 5. 应用干预
    context = {"existing": "data"}
    result = await loop.apply_intervention(context, triggered[0])
    assert result["emotion_offset"] == -0.2  # -0.4 * 0.5
    assert result["prompt_modifier"] == 0.1  # 0.2 * 0.5
    assert result["existing"] == "data"  # 原有数据保留

    # 6. 验证收敛指标
    metrics = loop.get_convergence_metrics()
    assert metrics["intervention_count"] == 1


@pytest.mark.asyncio
async def test_e2e_convergence_over_multiple_interventions():
    """多次干预后验证收敛趋势"""
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule(
        "cognitive_load", threshold=0.5, direction_name="calm",
        alpha=0.5, cooldown=0.0,
    ))

    # 模拟 score 逐渐下降的干预序列
    scores = [0.9, 0.8, 0.7, 0.6, 0.5]
    for score in scores:
        await stream.emit("cognitive_load", score, "test")
        await loop.evaluate({})

    metrics = loop.get_convergence_metrics()
    assert metrics["intervention_count"] >= 5
    assert metrics["converging"] is True  # score 在下降


@pytest.mark.asyncio
async def test_e2e_no_intervention_below_threshold():
    """低于阈值时不触发干预"""
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule(
        "cognitive_load", threshold=0.8, direction_name="calm", alpha=0.5,
    ))

    await stream.emit("cognitive_load", 0.3, "test")
    triggered = await loop.evaluate({})
    assert len(triggered) == 0


@pytest.mark.asyncio
async def test_e2e_non_blocking_on_failure():
    """模块失败时不阻塞主流程"""
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()  # 空注册表，不注册任何方向
    loop = InterventionLoop(stream, registry)
    loop.register_rule(InterventionRule(
        "cognitive_load", threshold=0.5, direction_name="nonexistent", alpha=0.5,
    ))

    await stream.emit("cognitive_load", 0.9, "test")
    triggered = await loop.evaluate({})
    # 方向不存在时应跳过，不崩溃
    assert len(triggered) == 0
