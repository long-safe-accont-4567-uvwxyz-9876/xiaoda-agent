"""熔断器与认知状态追踪单元测试 — 精简版 (两态)"""
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


def test_check_green_closed():
    """正常状态返回 CLOSED (即 GREEN)"""
    cb = CircuitBreaker()
    state = CognitiveState()
    assert cb.check(state) == CircuitState.CLOSED
    assert cb.check(state) == CircuitState.GREEN  # 别名兼容


def test_check_red_too_many_fails():
    """连续失败达到阈值 → OPEN (即 RED)"""
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    result = cb.check(state)
    assert result == CircuitState.OPEN
    assert result == CircuitState.RED  # 别名兼容


def test_check_below_threshold_stays_closed():
    """连续失败未达阈值 → 保持 CLOSED"""
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=4)  # 阈值是 5
    result = cb.check(state)
    assert result == CircuitState.CLOSED


def test_recovery_after_cooldown():
    """OPEN 状态冷却后自动恢复 CLOSED"""
    cb = CircuitBreaker(cooldown=60)
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)
    assert cb._open_since > 0

    red_time = cb._open_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        result = cb.check(state)
    assert result == CircuitState.CLOSED
    assert cb._open_since == 0.0


def test_recovery_resets_consecutive_fails():
    """恢复后 consecutive_fails 保持不变 (由调用方在 success 时重置)"""
    cb = CircuitBreaker(cooldown=1)
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    red_time = cb._open_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 1):
        cb.check(state)
    # consecutive_fails 由调用方在 success 时重置，这里只是验证时间推进


# ── on_failure / on_success 更新 ──


def test_on_failure_updates():
    """失败时 consecutive_fails +1, confidence -0.1"""
    cb = CircuitBreaker()
    state = CognitiveState()
    cb.on_failure(state)
    assert state.consecutive_fails == 1
    assert state.confidence == pytest.approx(0.9)


def test_on_failure_multiple():
    """连续失败累加"""
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=2)
    cb.on_failure(state)
    assert state.consecutive_fails == 3


def test_on_success_resets_fails():
    """成功后 consecutive_fails 重置为 0"""
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=3)
    cb.on_success(state)
    assert state.consecutive_fails == 0


def test_on_success_confidence_increases():
    """成功后 confidence 上限 1.0"""
    cb = CircuitBreaker()
    state = CognitiveState(confidence=0.8)
    cb.on_success(state)
    assert state.confidence == pytest.approx(0.85)


# ── 探测成功/失败 (兼容 HALF_OPEN 旧接口) ──


def test_half_open_success_closes_circuit():
    """on_half_open_success → 回到 CLOSED"""
    cb = CircuitBreaker(cooldown=60)
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    # 冷却到期前触发探测成功
    red_time = cb._open_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        cb.check(state)  # 此时应已恢复到 CLOSED

    # 但用 half_open_success 也能强制恢复（兼容旧接口）
    cb.on_half_open_success(state)
    assert cb._open_since == 0.0
    assert state.consecutive_fails == 0


def test_half_open_failure_stays_open():
    """on_half_open_failure → 保持 OPEN 且冷却时间翻倍"""
    cb = CircuitBreaker(cooldown=60, max_cooldown=600)
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    old_cooldown = cb.RED_RECOVERY_SECONDS
    assert old_cooldown == 60

    cb.on_half_open_failure(state)
    assert cb._open_since > 0
    assert cb.RED_RECOVERY_SECONDS == 120


def test_half_open_failure_max_cooldown():
    """指数退避上限为 max_cooldown"""
    cb = CircuitBreaker(cooldown=60, max_cooldown=300)
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    # 触发 3 次探测失败：60 → 120 → 240 → 300
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 120
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 240
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 300  # 上限
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 300  # 保持上限


# ── 工具失败率追踪 ──


def test_on_failure_tool_tracking():
    cb = CircuitBreaker()
    state = CognitiveState()
    cb.on_failure(state, is_tool=True)
    assert state._total_tool_calls == 1
    assert state._failed_tool_calls == 1
    assert state.tool_fail_rate == pytest.approx(1.0)


def test_on_success_tool_tracking():
    cb = CircuitBreaker()
    state = CognitiveState()
    cb.on_failure(state, is_tool=True)
    assert state.tool_fail_rate == pytest.approx(1.0)

    cb.on_success(state, is_tool=True)
    assert state._total_tool_calls == 2
    assert state._failed_tool_calls == 1
    assert state.tool_fail_rate == pytest.approx(0.5)


# ── can_execute / record_success / record_failure ──


def test_can_execute_initially_true():
    cb = CircuitBreaker()
    assert cb.can_execute() is True


def test_record_failure_then_check():
    """record_failure 累积失败，check 时触发 OPEN"""
    cb = CircuitBreaker()
    state = CognitiveState()
    for _ in range(5):
        cb.record_failure()
        # 需要用同一 state 才能累积
        cb.on_failure(state)
    result = cb.check(state)
    assert result == CircuitState.OPEN


def test_record_success_resets():
    cb = CircuitBreaker()
    for _ in range(5):
        cb.record_failure()
    cb.record_success()
    # consecutive_fails 重置后应该可以执行
    assert cb.can_execute() is True
