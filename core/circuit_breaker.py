"""熔断器与认知状态追踪 — 6信号熔断，三级状态"""
import time
import logging
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断状态：GREEN 正常 / YELLOW 预警 / RED 熔断 / HALF_OPEN 探测"""
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    HALF_OPEN = "half_open"


@dataclass
class CognitiveState:
    """认知状态追踪 — 五维状态"""
    confidence: float = 1.0        # 信心 0~1
    fatigue: float = 0.0           # 疲劳 0~1
    deviation: float = 0.0         # 偏差积累 0~1
    consecutive_fails: int = 0     # 连续失败次数
    tool_fail_rate: float = 0.0    # 工具失败率
    # 内部追踪
    _total_tool_calls: int = 0
    _failed_tool_calls: int = 0
    _red_since: float = 0.0        # RED 状态开始时间


class CircuitBreaker:
    """熔断器 — 6信号检测，三级熔断"""

    THRESHOLDS = {
        "consecutive_fails_yellow": 2,
        "consecutive_fails_red": 5,
        "confidence_yellow": 0.5,
        "confidence_red": 0.2,
        "fatigue_yellow": 0.5,
        "fatigue_red": 0.8,
        "deviation_yellow": 0.3,
        "deviation_red": 0.6,
        "tool_fail_rate_yellow": 0.3,
        "tool_fail_rate_red": 0.6,
    }

    RED_RECOVERY_SECONDS = 60

    def check(self, state: CognitiveState) -> CircuitState:
        if state._red_since > 0:
            if time.time() - state._red_since >= self.RED_RECOVERY_SECONDS:
                return CircuitState.HALF_OPEN
            return CircuitState.RED

        red_signals = 0
        yellow_signals = 0

        if state.consecutive_fails >= self.THRESHOLDS["consecutive_fails_red"]:
            red_signals += 1
        elif state.consecutive_fails >= self.THRESHOLDS["consecutive_fails_yellow"]:
            yellow_signals += 1

        if state.confidence <= self.THRESHOLDS["confidence_red"]:
            red_signals += 1
        elif state.confidence <= self.THRESHOLDS["confidence_yellow"]:
            yellow_signals += 1

        if state.fatigue >= self.THRESHOLDS["fatigue_red"]:
            red_signals += 1
        elif state.fatigue >= self.THRESHOLDS["fatigue_yellow"]:
            yellow_signals += 1

        if state.deviation >= self.THRESHOLDS["deviation_red"]:
            red_signals += 1
        elif state.deviation >= self.THRESHOLDS["deviation_yellow"]:
            yellow_signals += 1

        if state.tool_fail_rate >= self.THRESHOLDS["tool_fail_rate_red"]:
            red_signals += 1
        elif state.tool_fail_rate >= self.THRESHOLDS["tool_fail_rate_yellow"]:
            yellow_signals += 1

        if red_signals >= 1:
            state._red_since = time.time()
            return CircuitState.RED
        elif yellow_signals >= 2:
            return CircuitState.YELLOW
        return CircuitState.GREEN

    def on_failure(self, state: CognitiveState, is_tool: bool = False):
        state.consecutive_fails += 1
        state.confidence = max(0.0, state.confidence - 0.1)
        state.fatigue = min(1.0, state.fatigue + 0.05)
        if is_tool:
            state._failed_tool_calls += 1
            state._total_tool_calls += 1
            state.tool_fail_rate = state._failed_tool_calls / max(1, state._total_tool_calls)
        state._red_since = 0.0

    def on_success(self, state: CognitiveState, is_tool: bool = False):
        state.consecutive_fails = 0
        state.confidence = min(1.0, state.confidence + 0.05)
        state.fatigue = max(0.0, state.fatigue - 0.02)
        state.deviation = max(0.0, state.deviation - 0.01)
        if is_tool:
            state._total_tool_calls += 1
            state.tool_fail_rate = state._failed_tool_calls / max(1, state._total_tool_calls)
        state._red_since = 0.0

    def on_half_open_success(self, state: CognitiveState):
        state._red_since = 0.0
        state.consecutive_fails = 0
        logger.info("circuit_breaker.recovered", extra={"state": "GREEN"})

    def on_half_open_failure(self, state: CognitiveState):
        state._red_since = time.time()
        logger.warning("circuit_breaker.half_open_failed")
