"""熔断器与认知状态追踪单元测试"""
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
    assert cb._red_since > 0


def test_check_red_very_low_confidence():
    cb = CircuitBreaker()
    state = CognitiveState(confidence=0.1)
    result = cb.check(state)
    assert result == CircuitState.RED
    assert cb._red_since > 0


def test_check_red_high_fatigue():
    cb = CircuitBreaker()
    state = CognitiveState(fatigue=0.9)
    result = cb.check(state)
    assert result == CircuitState.RED
    assert cb._red_since > 0


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
    assert cb._red_since > 0

    # mock time.time 使得距离 RED 已过 60 秒
    red_time = cb._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        result = cb.check(state)
    assert result == CircuitState.HALF_OPEN


def test_half_open_success_restores_green():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    # 进入 half_open
    red_time = cb._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        cb.check(state)

    # 探测成功
    cb.on_half_open_success(state)
    assert cb._red_since == 0.0
    assert state.consecutive_fails == 0

    # 再次 check 应该是 GREEN
    result = cb.check(state)
    assert result == CircuitState.GREEN


def test_half_open_failure_returns_red():
    cb = CircuitBreaker()
    state = CognitiveState(consecutive_fails=5)
    cb.check(state)

    # 进入 half_open
    red_time = cb._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        cb.check(state)

    # 探测失败
    with patch("core.circuit_breaker.time.time", return_value=red_time + 70):
        cb.on_half_open_failure(state)
    assert cb._red_since > 0

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


# ── 指数退避（Task 12.3）──


def test_exponential_backoff():
    """连续探测失败冷却时间翻倍，且不超过 MAX_COOLDOWN"""
    cb = CircuitBreaker(cooldown=60, max_cooldown=600)
    state = CognitiveState(consecutive_fails=5)
    assert cb.RED_RECOVERY_SECONDS == 60

    # 第一次探测失败：60 → 120
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 120

    # 第二次：120 → 240
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 240

    # 第三次：240 → 480
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 480

    # 第四次：480 → 600（被 MAX_COOLDOWN 截断）
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 600

    # 再失败仍为上限 600
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 600


def test_exponential_backoff_reset_on_success():
    """探测成功后冷却时间重置为初始值"""
    cb = CircuitBreaker(cooldown=60, max_cooldown=600)
    state = CognitiveState(consecutive_fails=5)
    # 累积退避到 240
    cb.on_half_open_failure(state)
    cb.on_half_open_failure(state)
    assert cb.RED_RECOVERY_SECONDS == 240

    # 探测成功 → 重置为初始 60
    cb.on_half_open_success(state)
    assert cb.RED_RECOVERY_SECONDS == 60


# ── P3: 探测成功恢复持久信号（防"探测成功但立刻再 RED"循环） ──


def test_half_open_success_restores_persistent_signals():
    """探测成功必须恢复 confidence/fatigue/deviation，否则下一条消息立刻再 RED。"""
    cb = CircuitBreaker()
    # 疲劳触发 RED（fatigue=0.85 > 0.8），而非连续失败
    state = CognitiveState(fatigue=0.85, confidence=0.3)
    assert cb.check(state) == CircuitState.RED

    red_time = cb._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        assert cb.check(state) == CircuitState.HALF_OPEN

    cb.on_half_open_success(state)
    # 恢复后疲劳回落到安全区（0.85 - 0.2 = 0.65 < 0.8），置信度回升
    assert state.fatigue < cb.THRESHOLDS["fatigue_red"]
    assert state.confidence > 0.3

    # 下一条消息 check() 不再立刻 RED（回归锁定：8-15 日志实证的循环）
    result = cb.check(state)
    assert result != CircuitState.RED


def test_half_open_success_heals_confidence_triggered_red():
    """置信度触发的 RED 在探测成功后不再复发。"""
    cb = CircuitBreaker()
    state = CognitiveState(confidence=0.1, fatigue=0.7)
    assert cb.check(state) == CircuitState.RED

    red_time = cb._red_since
    with patch("core.circuit_breaker.time.time", return_value=red_time + 60):
        assert cb.check(state) == CircuitState.HALF_OPEN
    cb.on_half_open_success(state)
    assert cb.check(state) == CircuitState.GREEN


def test_red_signal_logs_dimension_details():
    """RED 时日志必须带触发维度明细（可观测性）。"""
    from loguru import logger as _logger

    records = []

    class _Sink:
        def write(self, message):
            records.append(message)

    sink_id = _logger.add(
        _Sink(),
        format="{extra[fatigue]} {extra[fatigue_threshold]} "
               "{extra[confidence]} {extra[confidence_threshold]} {message}",
    )
    try:
        cb = CircuitBreaker()
        state = CognitiveState(fatigue=0.9, confidence=0.2)
        cb.check(state)
    finally:
        _logger.remove(sink_id)

    joined = "".join(records)
    assert "0.9" in joined
    assert "0.8" in joined  # fatigue_threshold
    assert "0.2" in joined  # confidence
    assert "circuit_breaker.state_change" in joined
