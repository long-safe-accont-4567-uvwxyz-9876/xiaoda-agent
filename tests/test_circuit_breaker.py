"""熔断器与认知状态追踪单元测试"""
import time
from unittest.mock import patch

import pytest

from core.circuit_breaker import CircuitBreaker, CircuitState, CognitiveState


# ── CognitiveState 默认值 ──


def test_cognitive_state_defaults():
    state = CognitiveState()
    assert state.confidence == 1.0
    assert state.fatigue == 0.0
    assert state.deviation == 0.0
    assert state.consecutive_fails == 0
    assert state.tool_fail_rate == 0.0


# ── CircuitBreaker.check ──


def test_check_green():
    cb = CircuitBreaker()
    state = CognitiveState()
    assert cb.check(state) == CircuitState.GREEN


def test_check_yellow_consecutive_fails():
    cb = CircuitBreaker()
    # consecutive_fails=2 是黄色信号，还需另一个黄色信号（fatigue=0.5）才能达到 yellow_signals>=2
    state = CognitiveState(consecutive_fails=2, fatigue=0.5)
    assert cb.check(state) == CircuitState.YELLOW


def test_check_yellow_low_confidence():
    cb = CircuitBreaker()
    # confidence=0.4 是黄色信号，还需另一个黄色信号（fatigue=0.5）才能达到 yellow_signals>=2
    state = CognitiveState(confidence=0.4, fatigue=0.5)
    assert cb.check(state) == CircuitState.YELLOW


def test_check_yellow_high_fatigue():
    cb = CircuitBreaker()
    # fatigue=0.6 是黄色信号，还需另一个黄色信号（consecutive_fails=2）才能达到 yellow_signals>=2
    state = CognitiveState(fatigue=0.6, consecutive_fails=2)
    assert cb.check(state) == CircuitState.YELLOW


def test_check_red_high_consecutive_fails():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    result = cb.check(state)
    assert result == CircuitState.RED
    assert state._red_since > 0


def test_check_red_very_low_confidence():
    cb = CircuitBreaker()
    state = CognitiveState(confidence=0.1)
    result = cb.check(state)
    assert result == CircuitState.RED
    assert state._red_since > 0


def test_check_red_high_fatigue():
    cb = CircuitBreaker()
    state = CognitiveState(fatigue=0.9)
    result = cb.check(state)
    assert result == CircuitState.RED
    assert state._red_since > 0


# ── on_failure / on_success 更新 ──


def test_on_failure_updates():
    cb = CircuitBreaker()
    state = CognitiveState()
    cb.on_failure(state)
    assert state.consecutive_fails == 1
    assert state.confidence == pytest.approx(0.9)
    assert state.fatigue == pytest.approx(0.05)


def test_on_success_updates():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=3, confidence=0.5, fatigue=0.1)
    cb.on_success(state)
    assert state.consecutive_fails == 0
    assert state.confidence == pytest.approx(0.55)
    assert state.fatigue == pytest.approx(0.08)


# ── HALF_OPEN 逻辑 ──


def test_half_open_recovery():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    # 首次 check 进入 RED
    cb.check(state)
    assert state._red_since > 0

    # mock time.time 使得距离 RED 已过 60 秒
    red_time = state._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        result = cb.check(state)
    assert result == CircuitState.HALF_OPEN


def test_half_open_success_restores_green():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    # 进入 half_open
    red_time = state._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        cb.check(state)

    # 探测成功
    cb.on_half_open_success(state)
    assert state._red_since == 0.0
    assert state.consecutive_fails == 0

    # 再次 check 应该是 GREEN
    result = cb.check(state)
    assert result == CircuitState.GREEN


def test_half_open_failure_returns_red():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    # 进入 half_open
    red_time = state._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        cb.check(state)

    # 探测失败
    with patch("core.circuit_breaker.time.time", return_value=red_time + 70):
        cb.on_half_open_failure(state)
    assert state._red_since > 0

    # 再次 check 应该还是 RED
    with patch("core.circuit_breaker.time.time", return_value=red_time + 71):
        result = cb.check(state)
    assert result == CircuitState.RED


# ── 工具失败率追踪 ──


def test_on_failure_tool_tracking():
    cb = CircuitBreaker()
    state = CognitiveState()
    cb.on_failure(state, is_tool=True)
    assert state._total_tool_calls == 1
    assert state._failed_tool_calls == 1
    assert state.tool_fail_rate == pytest.approx(1.0)

    cb.on_failure(state, is_tool=True)
    assert state._total_tool_calls == 2
    assert state._failed_tool_calls == 2
    assert state.tool_fail_rate == pytest.approx(1.0)


def test_on_success_tool_tracking():
    cb = CircuitBreaker()
    state = CognitiveState()
    # 先失败一次
    cb.on_failure(state, is_tool=True)
    assert state.tool_fail_rate == pytest.approx(1.0)

    # 再成功一次
    cb.on_success(state, is_tool=True)
    assert state._total_tool_calls == 2
    assert state._failed_tool_calls == 1
    assert state.tool_fail_rate == pytest.approx(0.5)
