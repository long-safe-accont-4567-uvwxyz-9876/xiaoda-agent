# core/intervention_loop.py
"""
干预闭环 — 对齐 reprobe 的 Monitor + Steerer 闭环模式。

设计参考:
- reprobe/monitor.py: Monitor.score() 阈值判断
- reprobe/steerer.py: Steerer._apply_projection() 干预应用
- jlens/fitting.py: fit() 的 mean_rel_change 收敛追踪
"""
from dataclasses import dataclass
import time
from collections import deque
from loguru import logger

from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry


@dataclass
class InterventionRule:
    """干预规则 — 对齐 reprobe/monitor.py 的监控配置。"""
    signal_type: str
    threshold: float
    direction_name: str
    alpha: float = 0.5
    mode: str = "projected"
    trigger_above: bool = True  # True=超过阈值触发, False=低于阈值触发
    cooldown: float = 30.0
    last_triggered: float = 0.0


class InterventionLoop:
    """
    干预闭环 — 对齐 reprobe Monitor + Steerer 的观测→干预→验证闭环。
    """

    def __init__(self, signal_stream: BehavioralSignalStream,
                 direction_registry: DirectionRegistry):
        self._stream = signal_stream
        self._registry = direction_registry
        self._rules: list[InterventionRule] = []
        self._intervention_history: deque[dict] = deque(maxlen=500)

    def register_rule(self, rule: InterventionRule) -> None:
        """注册干预规则"""
        self._rules.append(rule)

    async def evaluate(self, context: dict) -> list[dict]:
        """
        聚合信号 → 阈值判断 → 返回触发的干预列表。

        对齐 reprobe/monitor.py: Monitor.score() + Steerer 触发逻辑。
        """
        triggered = []
        now = time.time()

        for rule in self._rules:
            score = self._stream.aggregate(rule.signal_type, "mean_of_means")

            # 空 buffer 保护：aggregate 对空 buffer 返回 0.0，
            # 不应作为有效信号触发干预
            if score == 0.0:
                continue

            if rule.trigger_above and score <= rule.threshold:
                continue
            if not rule.trigger_above and score >= rule.threshold:
                continue

            # cooldown 检查
            if rule.cooldown > 0 and (now - rule.last_triggered) < rule.cooldown:
                continue

            direction = self._registry.get(rule.direction_name)
            if direction is None:
                logger.debug(f"intervention_loop.direction_not_found: {rule.direction_name}")
                continue

            scaled = direction * rule.alpha
            rule.last_triggered = now
            entry = {
                "rule": rule.signal_type,
                "score": score,
                "direction": rule.direction_name,
                "alpha": rule.alpha,
                "mode": rule.mode,
                "scaled_direction": scaled,
            }
            triggered.append(entry)
            self._intervention_history.append({
                "timestamp": now,
                "signal_type": rule.signal_type,
                "score": score,
                "threshold": rule.threshold,
                "direction": rule.direction_name,
                "alpha": rule.alpha,
            })

        return triggered

    async def apply_intervention(self, context: dict, intervention: dict) -> dict:
        """
        应用干预到上下文。

        对齐 reprobe/steerer.py: Steerer._apply_projection()
        """
        direction: DirectionVector = intervention["scaled_direction"]
        return direction.apply_to_context(context)

    def get_convergence_metrics(self) -> dict:
        """
        收敛指标 — 对齐 jlens/fitting.py: fit() 中的 mean_rel_change 追踪。
        """
        if len(self._intervention_history) < 2:
            return {"converging": True, "intervention_count": len(self._intervention_history)}

        recent = list(self._intervention_history)[-5:]
        scores = [h["score"] for h in recent]
        trend = scores[-1] - scores[0] if len(scores) >= 2 else 0
        return {
            "converging": trend < 0,
            "trend": trend,
            "intervention_count": len(self._intervention_history),
            "recent_scores": scores,
        }