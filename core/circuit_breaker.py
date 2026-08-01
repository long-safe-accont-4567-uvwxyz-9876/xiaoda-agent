"""熔断器 (精简版) — 简单失败计数 + CLOSED/OPEN 两态 + 定时恢复。

状态机：CLOSED → (失败阈值) → OPEN → (冷却到期) → CLOSED
"""
import time
import threading
from enum import Enum
from dataclasses import dataclass
from typing import Any
from loguru import logger

try:
    from config import CIRCUIT_BREAKER_COOLDOWN, CIRCUIT_BREAKER_MAX_COOLDOWN
except ImportError:
    CIRCUIT_BREAKER_COOLDOWN = 60
    CIRCUIT_BREAKER_MAX_COOLDOWN = 600


class CircuitState(Enum):
    """熔断状态：CLOSED 正常 / OPEN 熔断"""
    GREEN = "green"       # 兼容旧名
    YELLOW = "yellow"     # 兼容旧名 (不再使用)
    RED = "red"           # 兼容旧名 (等同 OPEN)
    HALF_OPEN = "half_open"  # 兼容旧名 (不再使用)
    CLOSED = "green"      # 新名, 值与 GREEN 相同
    OPEN = "red"          # 新名, 值与 RED 相同


@dataclass
class CognitiveState:
    """认知状态追踪 (兼容旧接口, 内部仅用 consecutive_fails)"""
    confidence: float = 1.0
    fatigue: float = 0.0
    deviation: float = 0.0
    consecutive_fails: int = 0
    tool_fail_rate: float = 0.0
    _total_tool_calls: int = 0
    _failed_tool_calls: int = 0


class CircuitBreaker:
    """熔断器 (精简版) — 失败计数 + 两态 + 定时恢复。

    保留接口: check() / on_failure() / on_success() / on_half_open_success() / on_half_open_failure()
    兼容 CognitiveState 参数签名。
    """

    FAIL_THRESHOLD = 5  # 连续失败次数阈值

    def __init__(self,
                 cooldown: int | None = None,
                 half_open_probes: int | None = None,
                 max_cooldown: int | None = None) -> None:
        self._initial_cooldown = int(cooldown) if cooldown is not None else int(CIRCUIT_BREAKER_COOLDOWN)
        self._max_cooldown = int(max_cooldown) if max_cooldown is not None else int(CIRCUIT_BREAKER_MAX_COOLDOWN)
        self._current_cooldown = self._initial_cooldown
        self._open_since: float = 0.0
        self._last_state = CircuitState.CLOSED
        self._lock = threading.Lock()

    @property
    def RED_RECOVERY_SECONDS(self) -> int:
        """向后兼容：返回当前冷却时间"""
        return self._current_cooldown

    def _log_state_change(self, old: Any, new: Any, **kwargs: Any) -> None:
        extra = {"old": getattr(old, "value", old), "new": getattr(new, "value", new)}
        extra.update(kwargs)
        logger.info("circuit_breaker.state_change", **extra)

    def can_execute(self) -> bool:
        """检查是否允许执行 (简化版入口)"""
        state = CognitiveState()
        return self.check(state) != CircuitState.OPEN

    def record_success(self) -> None:
        """记录成功"""
        state = CognitiveState()
        self.on_success(state)

    def record_failure(self) -> None:
        """记录失败"""
        state = CognitiveState()
        self.on_failure(state)

    def check(self, state: CognitiveState) -> CircuitState:
        """检查熔断状态"""
        with self._lock:
            if self._open_since > 0:
                elapsed = time.time() - self._open_since
                if elapsed >= self._current_cooldown:
                    # 冷却到期 → 恢复 CLOSED
                    self._open_since = 0.0
                    self._current_cooldown = self._initial_cooldown
                    if self._last_state != CircuitState.CLOSED:
                        self._log_state_change(self._last_state, CircuitState.CLOSED,
                                               reason="cooldown_elapsed", elapsed=round(elapsed, 2))
                    self._last_state = CircuitState.CLOSED
                    return CircuitState.CLOSED
                return CircuitState.OPEN

            if state.consecutive_fails >= self.FAIL_THRESHOLD:
                self._open_since = time.time()
                self._log_state_change(self._last_state, CircuitState.OPEN,
                                       reason="fail_threshold",
                                       fails=state.consecutive_fails,
                                       cooldown=self._current_cooldown)
                self._last_state = CircuitState.OPEN
                return CircuitState.OPEN

            if self._last_state != CircuitState.CLOSED:
                self._log_state_change(self._last_state, CircuitState.CLOSED, reason="recovered")
                self._last_state = CircuitState.CLOSED
            return CircuitState.CLOSED

    def on_failure(self, state: CognitiveState, is_tool: bool = False) -> None:
        """失败时更新状态"""
        with self._lock:
            state.consecutive_fails += 1
            state.confidence = max(0.0, state.confidence - 0.1)
            if is_tool:
                state._failed_tool_calls += 1
                state._total_tool_calls += 1
                state.tool_fail_rate = state._failed_tool_calls / max(1, state._total_tool_calls)

    def on_success(self, state: CognitiveState, is_tool: bool = False) -> None:
        """成功时更新状态"""
        with self._lock:
            state.consecutive_fails = 0
            state.confidence = min(1.0, state.confidence + 0.05)
            if is_tool:
                state._total_tool_calls += 1
                state.tool_fail_rate = state._failed_tool_calls / max(1, state._total_tool_calls)

    def on_half_open_success(self, state: CognitiveState) -> None:
        """兼容旧接口 — 等同 on_success + 恢复"""
        with self._lock:
            self._open_since = 0.0
            state.consecutive_fails = 0
            self._current_cooldown = self._initial_cooldown
            self._log_state_change(CircuitState.OPEN, CircuitState.CLOSED, reason="probe_success")
            self._last_state = CircuitState.CLOSED

    def on_half_open_failure(self, state: CognitiveState) -> None:
        """兼容旧接口 — 回到 OPEN 并指数退避"""
        with self._lock:
            self._open_since = time.time()
            old_cooldown = self._current_cooldown
            self._current_cooldown = min(self._current_cooldown * 2, self._max_cooldown)
            self._log_state_change(CircuitState.OPEN, CircuitState.OPEN,
                                   reason="probe_failure",
                                   old_cooldown=old_cooldown, new_cooldown=self._current_cooldown)
            self._last_state = CircuitState.OPEN
