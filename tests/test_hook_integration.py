# tests/test_hook_integration.py (partial — will be extended in Task 11)
"""Hook 集成测试 — agent_introspection + behavioral_health 信号采集

覆盖:
- Hook #1: AgentIntrospector.get_current_state() 应 emit cognitive_load 信号
- Hook #6: BehavioralHealthScorer.calculate() 应 emit health 信号
- 非阻塞约束: _signal_stream 为 None / emit 失败时不影响主流程
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.behavioral_signal import BehavioralSignalStream

# ============================================================
# 辅助 fixtures (复用 test_agent_introspection.py 的 fake 模式)
# ============================================================

class _FakeMetacog:
    """模拟 MetacognitionLite, 带 get_state_dict"""

    def __init__(self, confidence=0.8, uncertainty=0.6, drift_score=0.4,
                 phase="monitor"):
        self._dict = {
            "phase": phase,
            "confidence": confidence,
            "uncertainty": uncertainty,
            "drift_score": drift_score,
        }

    def get_state_dict(self):
        return dict(self._dict)


# ============================================================
# Hook #1: agent_introspection
# ============================================================

@pytest.mark.asyncio
async def test_hook_agent_introspection_emits_signal():
    """Hook #1: get_current_state() 应 emit cognitive_load 信号 (source=introspection)"""
    stream = BehavioralSignalStream()

    from core.agent_introspection import AgentIntrospector

    # 构造带 metacog 的 introspector, 使 cognitive_load 有非零值
    # cognitive_load = 0.5 * uncertainty + 0.5 * drift_score = 0.5 * 0.6 + 0.5 * 0.4 = 0.5
    mc = _FakeMetacog(uncertainty=0.6, drift_score=0.4)
    agent = SimpleNamespace(metacognition=mc, _start_time=time.time())
    intro = AgentIntrospector(agent=agent)

    with patch("core.agent_introspection._signal_stream", stream):
        state = intro.get_current_state()
        # 让 create_task 完成 (hook 用 loop.create_task 异步发射)
        await asyncio.sleep(0.05)

    # 验证 cognitive_load 被正确计算
    expected_load = 0.5 * 0.6 + 0.5 * 0.4
    assert state.cognitive_load == pytest.approx(expected_load, abs=0.01)

    # 验证信号已发射
    history = stream.get_history("cognitive_load")
    assert len(history) >= 1, "cognitive_load signal not emitted"
    assert history[0].signal_type == "cognitive_load"
    assert history[0].source == "introspection"
    assert history[0].value == pytest.approx(expected_load, abs=0.01)


@pytest.mark.asyncio
async def test_hook_agent_introspection_no_signal_when_stream_none():
    """Hook #1 非阻塞: _signal_stream 为 None 时不崩溃, 不发射"""
    from core.agent_introspection import AgentIntrospector

    # 模块默认 _signal_stream = None (未 patch)
    intro = AgentIntrospector()
    state = intro.get_current_state()  # 不应抛
    assert state is not None

    # 让出控制权, 确认无任务挂起
    await asyncio.sleep(0.01)


# ============================================================
# Hook #6: behavioral_health
# ============================================================

@pytest.mark.asyncio
async def test_hook_behavioral_health_emits_signal():
    """Hook #6: BehavioralHealthScorer.calculate() 应 emit health 信号"""
    stream = BehavioralSignalStream()

    from core.behavioral_health import BehavioralHealthScorer
    scorer = BehavioralHealthScorer()
    metrics = {
        "p50_latency_ms": 500,
        "p99_latency_ms": 800,
        "success_rate": 0.97,
        "error_rate": 0.005,
        "memory_usage": 0.40,
        "tool_success_rate": 0.98,
    }

    with patch("core.behavioral_health._signal_stream", stream):
        score = scorer.calculate(metrics)
        # 让 create_task 完成
        await asyncio.sleep(0.05)

    # 验证评分正确
    assert score.score == 5

    # 验证信号已发射
    history = stream.get_history("health")
    assert len(history) >= 1, "health signal not emitted"
    assert history[0].signal_type == "health"
    assert history[0].source == "behavioral_health"
    assert history[0].value == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_hook_behavioral_health_no_signal_when_stream_none():
    """Hook #6 非阻塞: _signal_stream 为 None 时不崩溃, 不发射"""
    from core.behavioral_health import BehavioralHealthScorer

    # 模块默认 _signal_stream = None (未 patch)
    scorer = BehavioralHealthScorer()
    score = scorer.calculate({"p99_latency_ms": 800})  # 不应抛
    assert score is not None

    await asyncio.sleep(0.01)


# ============================================================
# 非阻塞约束: emit 失败时不影响主流程
# ============================================================

@pytest.mark.asyncio
async def test_hook_non_blocking_on_emit_failure():
    """Hook 非阻塞: emit 抛异常时主流程仍正常完成"""
    from core.agent_introspection import AgentIntrospector
    from core.behavioral_health import BehavioralHealthScorer

    class _BrokenStream:
        async def emit(self, *args, **kwargs):
            raise RuntimeError("broken stream")

    broken = _BrokenStream()

    # agent_introspection: get_current_state 不应抛
    with patch("core.agent_introspection._signal_stream", broken):
        intro = AgentIntrospector()
        state = intro.get_current_state()
        assert state is not None
        await asyncio.sleep(0.05)  # 让 create_task 完成 (异常被 task 吞掉)

    # behavioral_health: calculate 不应抛
    with patch("core.behavioral_health._signal_stream", broken):
        scorer = BehavioralHealthScorer()
        score = scorer.calculate({"p99_latency_ms": 800})
        assert score is not None
        await asyncio.sleep(0.05)


# ============================================================
# C1 regression: bootstrap wiring
# ============================================================

@pytest.mark.asyncio
async def test_bootstrap_wires_hooks():
    """C1 regression: init_j_space() must wire components to all Hook modules."""
    import agent_dispatcher as _ad
    import belief_router as _br
    import core.agent_introspection as _ai
    import core.behavioral_health as _bh
    import core.degradation_strategy as _ds
    import memory.cognitive_memory as _cm
    from core.j_space_bootstrap import get_intervention_loop, get_signal_stream, init_j_space

    # Save original state
    orig = {
        "ai": _ai._signal_stream,
        "bh": _bh._signal_stream,
        "ad_s": _ad._signal_stream,
        "ad_i": _ad._intervention_loop,
        "ds": _ds._signal_stream,
        "cm": _cm._structured_blackboard,
        "br": _br._enhanced_router,
    }
    try:
        init_j_space()

        # Bootstrap components created
        assert get_signal_stream() is not None
        assert get_intervention_loop() is not None

        # Hook modules wired
        assert _ai._signal_stream is not None
        assert _bh._signal_stream is not None
        assert _ad._signal_stream is not None
        assert _ad._intervention_loop is not None
        assert _ds._signal_stream is not None
        assert _cm._structured_blackboard is not None
        assert _br._enhanced_router is not None
    finally:
        # Restore original state to avoid polluting other tests
        _ai._signal_stream = orig["ai"]
        _bh._signal_stream = orig["bh"]
        _ad._signal_stream = orig["ad_s"]
        _ad._intervention_loop = orig["ad_i"]
        _ds._signal_stream = orig["ds"]
        _cm._structured_blackboard = orig["cm"]
        _br._enhanced_router = orig["br"]
