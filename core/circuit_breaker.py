"""熔断器与认知状态追踪 — 6信号熔断，三级状态 + 半开探测智能恢复。

状态机：GREEN/YELLOW → RED → (冷却到期) → HALF_OPEN → 探测
    探测成功 → GREEN（冷却时间重置为初始值）
    探测失败 → RED（冷却时间指数退避，上限 MAX_COOLDOWN）
"""
from typing import Any
import time
import threading
from enum import Enum
from dataclasses import dataclass

from loguru import logger

try:
    from config import (
        CIRCUIT_BREAKER_COOLDOWN,
        CIRCUIT_BREAKER_HALF_OPEN_PROBES,
        CIRCUIT_BREAKER_MAX_COOLDOWN,
    )
except ImportError:  # pragma: no cover - config 未加载时回退默认值
    CIRCUIT_BREAKER_COOLDOWN = 60
    CIRCUIT_BREAKER_HALF_OPEN_PROBES = 1
    CIRCUIT_BREAKER_MAX_COOLDOWN = 600


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


class CircuitBreaker:
    """熔断器 — 6信号检测，三级熔断 + 半开探测智能恢复。

    Task 11: HALF_OPEN 状态机 —— 冷却到期后允许有限探测请求通过，
             探测成功恢复 GREEN，探测失败回到 RED。
    Task 12: 自适应恢复 —— 冷却时间指数退避（每次失败 *2，上限 MAX_COOLDOWN），
             探测请求数可配置。
    Task 13: loguru 记录状态切换与探测结果。
    """

    # 阈值配置
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

    def __init__(self,
                 cooldown: int = None,
                 half_open_probes: int = None,
                 max_cooldown: int = None) -> None:
        # Task 12: 自适应恢复参数
        self._initial_cooldown = int(cooldown) if cooldown is not None else int(CIRCUIT_BREAKER_COOLDOWN)
        self._half_open_probes = int(half_open_probes) if half_open_probes is not None else int(CIRCUIT_BREAKER_HALF_OPEN_PROBES)
        self._max_cooldown = int(max_cooldown) if max_cooldown is not None else int(CIRCUIT_BREAKER_MAX_COOLDOWN)
        # 当前冷却时间（指数退避后会增大，探测成功后重置）
        self._current_cooldown = self._initial_cooldown
        # Task 11.3: 探测限流 —— half_open 期间允许的探测配额
        self._probes_in_flight = 0
        self._probes_used = 0
        self._probe_start_time = 0.0
        # 上一次返回的逻辑状态（用于状态切换日志）
        self._last_state = CircuitState.GREEN
        # Task 11.1: RED 状态开始时间（归属熔断器而非外部传入的认知状态）
        self._red_since: float = 0.0
        # 线程安全：方法为同步调用，使用 threading.Lock 保护可变状态
        self._lock = threading.Lock()

    @property
    def RED_RECOVERY_SECONDS(self) -> int:
        """向后兼容：返回当前冷却时间（指数退避后可能增大）"""
        return self._current_cooldown

    # ── 内部辅助 ──

    def _log_state_change(self, old: Any, new: Any, **kwargs: Any) -> None:
        """Task 13.1: 状态切换日志"""
        extra = {
            "old": getattr(old, "value", old),
            "new": getattr(new, "value", new),
        }
        extra.update(kwargs)
        logger.info("circuit_breaker.state_change", **extra)

    def _reset_probes(self) -> None:
        """重置探测计数（探测本轮结束）"""
        self._probes_in_flight = 0
        self._probes_used = 0
        self._probe_start_time = 0.0

    def _count_signals(self, state: CognitiveState) -> tuple:
        """统计红/黄信号数量"""
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

        return red_signals, yellow_signals

    # ── 公共 API（签名保持向后兼容） ──

    def check(self, state: CognitiveState) -> CircuitState:
        """检查熔断状态"""
        with self._lock:
            # 如果当前是 RED，检查是否可以进入 half-open
            if self._red_since > 0:
                elapsed = time.time() - self._red_since
                if elapsed >= self._current_cooldown:
                    # Task 11.2: 冷却到期 → 尝试进入 half-open
                    # 安全网：若探测卡住（在途时间超过冷却），重置配额
                    if self._probes_in_flight > 0 and self._probe_start_time > 0:
                        if time.time() - self._probe_start_time >= self._current_cooldown:
                            self._reset_probes()
                    # Task 11.3: 仅当探测配额未用完时允许探测请求通过
                    if self._probes_used < self._half_open_probes:
                        self._probes_in_flight += 1
                        self._probes_used += 1
                        self._probe_start_time = time.time()
                        self._log_state_change(
                            CircuitState.RED, CircuitState.HALF_OPEN,
                            reason="cooldown_elapsed",
                            elapsed=round(elapsed, 2),
                            cooldown=self._current_cooldown,
                            probe_index=self._probes_used,
                        )
                        self._last_state = CircuitState.HALF_OPEN
                        return CircuitState.HALF_OPEN
                    # 探测配额已用完，其他请求继续拒绝
                    return CircuitState.RED
                return CircuitState.RED

            red_signals, yellow_signals = self._count_signals(state)

            if red_signals >= 1:
                self._red_since = time.time()
                self._log_state_change(
                    self._last_state, CircuitState.RED,
                    reason="red_signal", red_signals=red_signals,
                    cooldown=self._current_cooldown,
                )
                self._last_state = CircuitState.RED
                return CircuitState.RED
            elif yellow_signals >= 2:
                if self._last_state != CircuitState.YELLOW:
                    self._log_state_change(
                        self._last_state, CircuitState.YELLOW,
                        reason="yellow_signals", yellow_signals=yellow_signals,
                    )
                    self._last_state = CircuitState.YELLOW
                return CircuitState.YELLOW

            if self._last_state != CircuitState.GREEN:
                self._log_state_change(
                    self._last_state, CircuitState.GREEN,
                    reason="recovered",
                )
                self._last_state = CircuitState.GREEN
            return CircuitState.GREEN

    def on_failure(self, state: CognitiveState, is_tool: bool = False) -> None:
        """失败时更新状态"""
        with self._lock:
            state.consecutive_fails += 1
            state.confidence = max(0.0, state.confidence - 0.1)
            state.fatigue = min(1.0, state.fatigue + 0.05)
            if is_tool:
                state._failed_tool_calls += 1
                state._total_tool_calls += 1
                state.tool_fail_rate = state._failed_tool_calls / max(1, state._total_tool_calls)
            # 清除 RED 状态标记（让 check 重新判定）
            self._red_since = 0.0

    def on_success(self, state: CognitiveState, is_tool: bool = False) -> None:
        """成功时更新状态"""
        with self._lock:
            state.consecutive_fails = 0
            state.confidence = min(1.0, state.confidence + 0.05)
            state.fatigue = max(0.0, state.fatigue - 0.02)
            state.deviation = max(0.0, state.deviation - 0.01)
            if is_tool:
                state._total_tool_calls += 1
                state.tool_fail_rate = state._failed_tool_calls / max(1, state._total_tool_calls)
            # 成功后清除 RED 状态
            self._red_since = 0.0

    def on_half_open_success(self, state: CognitiveState) -> None:
        """Task 11.4: half-open 探测成功，恢复 GREEN"""
        with self._lock:
            elapsed = 0.0
            if self._probe_start_time > 0:
                elapsed = max(0.0, time.time() - self._probe_start_time)
            self._red_since = 0.0
            state.consecutive_fails = 0
            # Task 12.1: 恢复成功 → 冷却时间重置为初始值
            self._current_cooldown = self._initial_cooldown
            self._reset_probes()
            # Task 13.2: 探测结果日志
            logger.info(
                "circuit_breaker.probe_result",
                result="success", elapsed_ms=round(elapsed * 1000, 1),
            )
            self._log_state_change(
                CircuitState.HALF_OPEN, CircuitState.GREEN,
                reason="probe_success", elapsed_ms=round(elapsed * 1000, 1),
            )
            self._last_state = CircuitState.GREEN

    def on_half_open_failure(self, state: CognitiveState) -> None:
        """Task 11.4: half-open 探测失败，回到 RED 并指数退避"""
        with self._lock:
            elapsed = 0.0
            if self._probe_start_time > 0:
                elapsed = max(0.0, time.time() - self._probe_start_time)
            self._red_since = time.time()
            # Task 12.3: 连续恢复失败时冷却时间指数退避（cooldown *= 2），上限 MAX_COOLDOWN
            old_cooldown = self._current_cooldown
            self._current_cooldown = min(self._current_cooldown * 2, self._max_cooldown)
            self._reset_probes()
            # Task 13.2: 探测结果日志
            logger.info(
                "circuit_breaker.probe_result",
                result="failure", elapsed_ms=round(elapsed * 1000, 1),
            )
            self._log_state_change(
                CircuitState.HALF_OPEN, CircuitState.RED,
                reason="probe_failure",
                elapsed_ms=round(elapsed * 1000, 1),
                old_cooldown=old_cooldown, new_cooldown=self._current_cooldown,
            )
            self._last_state = CircuitState.RED
