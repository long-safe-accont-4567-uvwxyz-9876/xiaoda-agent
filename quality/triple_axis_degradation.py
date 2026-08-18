from __future__ import annotations

"""三轴退化模型 + 静默退化检测 — 2026关键洞察

三轴: Availability(二值) + Performance(连续) + Quality(最阴险)
检测 provider 偷偷切到弱模型的静默退化。
"""
from collections import deque
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class QualityProxy:
    """质量代理指标 — 不需要人工标注,从输出特征推断质量退化"""

    avg_response_length: float = 0      # 响应变短=退化信号
    schema_violation_rate: float = 0    # JSON Schema违反率飙升=退化
    refusal_rate: float = 0             # 拒绝回答率飙升=退化
    repetition_score: float = 0         # 输出重复度=退化
    hallucination_proxy: float = 0      # 幻觉代理(自相矛盾/与事实冲突)

    @property
    def quality_score(self) -> float:
        """0-1, 1=完美质量"""
        score = 1.0
        score -= 0.25 * min(1, self.schema_violation_rate * 10)
        score -= 0.25 * min(1, self.refusal_rate * 5)
        score -= 0.25 * min(1, self.repetition_score)
        score -= 0.25 * min(1, self.hallucination_proxy)
        return max(0, score)


@dataclass
class TripleAxisState:
    """三轴退化状态"""

    # Axis 1: Availability (二值)
    availability: bool = True
    last_success_time: float = 0

    # Axis 2: Performance (连续)
    latency_p50: float = 0
    latency_p95: float = 0
    latency_p99: float = 0

    # Axis 3: Quality (最阴险)
    quality: QualityProxy = field(default_factory=QualityProxy)

    # 上下文失忆检测
    context_amnesia_rate: float = 0

    @property
    def overall_health(self) -> float:
        """综合健康度 0-1"""
        if not self.availability:
            return 0.0
        perf_score = max(0, 1 - self.latency_p95 / 10000)
        return 0.3 * 1.0 + 0.4 * perf_score + 0.3 * self.quality.quality_score


class SilentDegradationDetector:
    """静默退化检测器 — 检测 provider 偷偷切到弱模型"""

    def __init__(self, baseline: TripleAxisState) -> None:
        self._baseline = baseline
        self._history: deque[TripleAxisState] = deque(maxlen=100)

    def check(self, current: TripleAxisState) -> list[str]:
        """检查是否有静默退化, 返回告警列表"""
        alerts = []
        self._history.append(current)

        # 质量退化超过20%但可用性正常=静默退化
        if (current.availability and
                current.quality.quality_score < self._baseline.quality.quality_score * 0.8):
            alerts.append(
                f"静默退化! Quality {current.quality.quality_score:.2f} < "
                f"baseline {self._baseline.quality.quality_score:.2f}*0.8"
            )

        # 上下文失忆
        if current.context_amnesia_rate > 0.3:
            alerts.append(f"上下文失忆: {current.context_amnesia_rate:.0%}的早期记忆丢失")

        # 延迟退化
        if current.latency_p95 > self._baseline.latency_p95 * 2:
            alerts.append(f"延迟退化: p95 {current.latency_p95:.0f}ms > baseline*2")

        for a in alerts:
            logger.warning(f"[三轴退化] {a}")

        return alerts

    def get_health_report(self) -> dict:
        """获取健康报告"""
        if not self._history:
            return {"status": "no_data"}
        latest = self._history[-1]
        return {
            "overall_health": round(latest.overall_health, 3),
            "availability": latest.availability,
            "latency_p50": latest.latency_p50,
            "latency_p95": latest.latency_p95,
            "quality_score": round(latest.quality.quality_score, 3),
            "context_amnesia_rate": latest.context_amnesia_rate,
        }
