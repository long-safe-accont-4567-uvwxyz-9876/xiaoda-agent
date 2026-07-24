"""异常行为检测 (S9) — 行为基线 + 偏离告警

参考:
- UEBA (User Entity Behavior Analytics)
- Statistical anomaly detection (Z-score, IQR)

特性:
- 维护行为基线 (滑动窗口 mean + std)
- 检测维度: 调用频率 / 工具选择 / 输入大小 / 错误率 / 时间分布
- 多级告警: info / warning / critical
- 自动基线更新 (EWMA)
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger


class Severity(str, Enum):
    """异常行为严重等级枚举。"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class BehaviorEvent:
    """行为事件"""
    user_id: str
    action: str                # tool_name / api_path
    timestamp: float = field(default_factory=time.time)
    duration: float = 0.0
    success: bool = True
    input_size: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class Anomaly:
    """异常"""
    severity: Severity
    user_id: str
    dimension: str              # 调用频率/工具选择/输入大小/错误率/时间
    observed: float
    expected: float
    z_score: float
    message: str
    timestamp: float = field(default_factory=time.time)


class BehaviorBaseline:
    """行为基线 (滑动窗口 + EWMA)"""

    def __init__(self, window_size: int = 100, alpha: float = 0.1) -> None:
        self._window: deque = deque(maxlen=window_size)
        self._ewma_mean: float = 0.0
        self._ewma_var: float = 0.0
        self._alpha = alpha
        self._n: int = 0

    def update(self, value: float) -> None:
        """更新基线"""
        self._window.append(value)
        self._n += 1
        # EWMA 方差更新
        delta = value - self._ewma_mean
        self._ewma_mean += self._alpha * delta
        self._ewma_var = (1 - self._alpha) * (self._ewma_var + self._alpha * delta * delta)

    @property
    def mean(self) -> float:
        """返回 EWMA 均值, 无样本时为 0."""
        return self._ewma_mean if self._n > 0 else 0.0

    @property
    def std(self) -> float:
        """返回 EWMA 标准差, 样本不足时为 0."""
        return math.sqrt(self._ewma_var) if self._n > 1 else 0.0

    def z_score(self, value: float) -> float:
        """计算 Z-score"""
        if self.std < 1e-9:
            return 0.0
        return (value - self._ewma_mean) / self.std

    @property
    def ready(self) -> bool:
        """返回基线是否已就绪 (样本数 >= 10)."""
        return self._n >= 10

    def seed(self, mean: float, std: float, n: int) -> None:
        """用历史统计量初始化基线（公共接口）。"""
        self._ewma_mean = float(mean)
        self._ewma_var = float(std) ** 2
        self._n = max(n, 1)


class AnomalyDetector:
    """异常行为检测器

    用法:
        det = AnomalyDetector()
        det.record(BehaviorEvent(user_id="u1", action="web_search"))
        anomalies = det.check(BehaviorEvent(user_id="u1", action="web_search"))
        for a in anomalies:
            print(a.severity, a.dimension, a.message)
    """

    def __init__(self, z_warn: float = 2.0, z_critical: float = 3.0) -> None:
        # 每用户每维度一个基线
        self._baselines: dict[tuple[str, str], BehaviorBaseline] = {}
        self._error_rates: dict[str, BehaviorBaseline] = {}
        self._z_warn = z_warn
        self._z_critical = z_critical
        self._anomalies: deque = deque(maxlen=1000)
        self._event_count = 0

    def _baseline(self, user_id: str, dimension: str) -> BehaviorBaseline:
        key = (user_id, dimension)
        if key not in self._baselines:
            self._baselines[key] = BehaviorBaseline()
        return self._baselines[key]

    def record(self, event: BehaviorEvent) -> None:
        """记录正常事件, 更新基线"""
        # 调用频率 (每分钟)
        self._baseline(event.user_id, "freq_per_min").update(1.0)
        # 输入大小
        if event.input_size > 0:
            self._baseline(event.user_id, "input_size").update(float(event.input_size))
        # 耗时
        if event.duration > 0:
            self._baseline(event.user_id, "duration").update(event.duration)
        # 错误率
        self._baseline(event.user_id, "error_rate").update(0.0 if event.success else 1.0)
        # 工具多样性 (本次 vs 历史)
        self._baseline(event.user_id, "tool_diversity").update(hash(event.action) % 10)
        self._event_count += 1

    def check(self, event: BehaviorEvent) -> list[Anomaly]:
        """检查事件是否异常"""
        anomalies: list[Anomaly] = []
        user = event.user_id

        # 1. 频率异常
        freq_b = self._baseline(user, "freq_per_min")
        if freq_b.ready:
            z = freq_b.z_score(1.0)
            if abs(z) > self._z_critical:
                anomalies.append(Anomaly(
                    severity=Severity.CRITICAL, user_id=user,
                    dimension="frequency", observed=1.0, expected=freq_b.mean,
                    z_score=z, message=f"Frequency anomaly: z={z:.2f}"
                ))
            elif abs(z) > self._z_warn:
                anomalies.append(Anomaly(
                    severity=Severity.WARNING, user_id=user,
                    dimension="frequency", observed=1.0, expected=freq_b.mean,
                    z_score=z, message=f"Frequency unusual: z={z:.2f}"
                ))

        # 2. 输入大小异常 (疑似 prompt injection)
        if event.input_size > 0:
            in_b = self._baseline(user, "input_size")
            if in_b.ready:
                z = in_b.z_score(float(event.input_size))
                if z > self._z_critical:
                    anomalies.append(Anomaly(
                        severity=Severity.CRITICAL, user_id=user,
                        dimension="input_size", observed=float(event.input_size),
                        expected=in_b.mean, z_score=z,
                        message=f"Input size anomaly: z={z:.2f} (potential prompt injection)"
                    ))

        # 3. 错误率突增
        err_b = self._baseline(user, "error_rate")
        if err_b.ready and not event.success:
            z = err_b.z_score(1.0)
            if z > self._z_critical:
                anomalies.append(Anomaly(
                    severity=Severity.CRITICAL, user_id=user,
                    dimension="error_rate", observed=1.0, expected=err_b.mean,
                    z_score=z, message=f"Error rate spike: z={z:.2f}"
                ))

        # 4. 时间分布异常 (凌晨高频调用)
        hour = time.localtime(event.timestamp).tm_hour
        if 0 <= hour < 6:
            anomalies.append(Anomaly(
                severity=Severity.INFO, user_id=user,
                dimension="time", observed=float(hour), expected=12.0,
                z_score=0.0, message=f"Off-hours activity: {hour}:00"
            ))

        # 记录异常
        for a in anomalies:
            self._anomalies.append(a)
            log_fn = logger.info if a.severity == Severity.INFO else \
                     logger.warning if a.severity == Severity.WARNING else logger.error
            log_fn(f"Anomaly.detected user={a.user_id} "
                    f"dim={a.dimension} sev={a.severity.value} "
                    f"z={a.z_score:.2f} msg={a.message}")

        return anomalies

    def get_recent_anomalies(self, limit: int = 50) -> list[Anomaly]:
        """返回最近 N 条异常记录.

        Args:
            limit: 返回的最大异常数, 默认 50

        Returns:
            最近的异常列表 (按时间升序)
        """
        return list(self._anomalies)[-limit:]

    def stats(self) -> dict:
        """返回检测器统计 (事件数/基线数/异常数/严重异常数)."""
        return {
            "event_count": self._event_count,
            "baseline_count": len(self._baselines),
            "anomaly_count": len(self._anomalies),
            "critical_count": sum(1 for a in self._anomalies if a.severity == Severity.CRITICAL),
        }


# 全局单例
_detector: AnomalyDetector | None = None


def get_anomaly_detector() -> AnomalyDetector:
    """获取全局 AnomalyDetector 单例, 不存在时创建."""
    global _detector
    if _detector is None:
        _detector = AnomalyDetector()
    return _detector
