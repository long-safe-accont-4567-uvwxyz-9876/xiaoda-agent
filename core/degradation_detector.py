"""三轴退化检测 + 静默退化告警 (Q2 P0 服务质量)

参考:
- Google SRE: 三轴退化 (Quality / Performance / Reliability)
- security/anomaly_detector.py 的 BehaviorBaseline (EWMA, alpha=0.1)

三轴指标:
- Quality       : 错误率 / 幻觉检测次数 / 用户负面反馈率
- Performance   : P50 / P99 延迟 / 吞吐量
- Reliability   : 成功率 / 重试率 / 超时率

退化判定:
- 单轴退化 (WARNING)   : 某轴指标偏离基线 > 2σ
- 双轴退化 (CRITICAL)  : 两轴同时退化
- 三轴退化 (EMERGENCY) : 三轴同时退化

静默退化检测:
- 无错误抛出但指标持续偏离基线 (连续 N 次 > 1σ, 未达到 2σ 退化阈值)

用法:
    det = DegradationDetector()
    det.record_quality("error_rate", 0.01)
    det.record_performance("p99_latency", 200.0)
    det.record_reliability("success_rate", 0.999)
    det.on_degradation(lambda r: alert(r))   # 注册回调
    det.start(interval=60)                    # 启动周期性后台检测
    report = det.check_degradation()          # 同步检查
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable

from loguru import logger

# 复用 anomaly_detector.BehaviorBaseline (EWMA, alpha=0.1)
from security.anomaly_detector import BehaviorBaseline


# ============================================================
# 枚举与数据结构
# ============================================================

class Axis(str, Enum):
    """退化轴"""
    QUALITY = "quality"
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"


class Severity(str, Enum):
    """退化严重级别"""
    NONE = "none"               # 无退化
    WARNING = "warning"          # 单轴退化 / 静默退化
    CRITICAL = "critical"        # 双轴退化
    EMERGENCY = "emergency"      # 三轴退化


@dataclass
class MetricDeviation:
    """单个指标的偏离信息"""
    name: str
    axis: Axis
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    silent: bool = False             # 是否属于静默退化 (未达 2σ 但持续 >1σ)


@dataclass
class DegradationReport:
    """退化检测报告"""
    axis: str                                    # "none" / "single" / "dual" / "triple"
    severity: Severity
    affected_axes: list[Axis] = field(default_factory=list)
    metrics: list[MetricDeviation] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    silent: bool = False                         # 是否检测到静默退化
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """将退化检测报告序列化为字典."""
        return {
            "axis": self.axis,
            "severity": self.severity.value,
            "affected_axes": [a.value for a in self.affected_axes],
            "metrics": [
                {
                    "name": m.name,
                    "axis": m.axis.value,
                    "value": m.value,
                    "baseline_mean": m.baseline_mean,
                    "baseline_std": m.baseline_std,
                    "z_score": m.z_score,
                    "silent": m.silent,
                }
                for m in self.metrics
            ],
            "recommendations": self.recommendations,
            "silent": self.silent,
            "timestamp": self.timestamp,
        }


# 三轴指标方向: True = 值越大越退化, False = 值越小越退化
AXIS_METRIC_DIRECTIONS: dict[Axis, dict[str, bool]] = {
    Axis.QUALITY: {
        "error_rate": True,                # 错误率
        "hallucination_count": True,       # 幻觉检测次数
        "negative_feedback_rate": True,    # 用户负面反馈率
    },
    Axis.PERFORMANCE: {
        "p50_latency": True,               # P50 延迟
        "p99_latency": True,               # P99 延迟
        "throughput": False,               # 吞吐量 (越低越退化)
    },
    Axis.RELIABILITY: {
        "success_rate": False,             # 成功率 (越低越退化)
        "retry_rate": True,               # 重试率
        "timeout_rate": True,             # 超时率
    },
}


# ============================================================
# 三轴退化检测器
# ============================================================

class DegradationDetector:
    """三轴退化检测器

    用法:
        det = DegradationDetector()
        det.record_quality("error_rate", 0.01)
        det.record_performance("p99_latency", 200.0)
        det.record_reliability("success_rate", 0.999)
        det.on_degradation(lambda r: send_alert(r))
        det.start(interval=60)              # 后台周期检测
        report = det.check_degradation()
    """

    def __init__(
        self,
        alpha: float = 0.1,
        z_degradation: float = 2.0,             # 退化阈值: |z| > 2σ
        z_silent: float = 1.0,                  # 静默偏离阈值: |z| > 1σ
        silent_consecutive: int = 5,            # 静默退化连续次数
        min_baseline_samples: int = 10,         # 基线 ready 阈值
        slo_tracker: object | None = None,   # 复用 SLOTracker (只读取)
        sla_exporter: object | None = None,  # 复用 SLAExporter (只读取)
    ) -> None:
        self._alpha = alpha
        self._z_degradation = z_degradation
        self._z_silent = z_silent
        self._silent_consecutive = silent_consecutive
        self._min_baseline_samples = min_baseline_samples
        # 每轴每指标的基线
        self._baselines: dict[Axis, dict[str, BehaviorBaseline]] = {
            Axis.QUALITY: {},
            Axis.PERFORMANCE: {},
            Axis.RELIABILITY: {},
        }
        # 每指标的连续静默偏离计数 (>1σ 次数, 未达 2σ)
        self._silent_counters: dict[Axis, dict[str, int]] = {
            Axis.QUALITY: {},
            Axis.PERFORMANCE: {},
            Axis.RELIABILITY: {},
        }
        # 每指标最新值
        self._latest: dict[Axis, dict[str, float]] = {
            Axis.QUALITY: {},
            Axis.PERFORMANCE: {},
            Axis.RELIABILITY: {},
        }
        self._callbacks: list[Callable[[DegradationReport], None]] = []
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._last_report: DegradationReport | None = None
        self._slo_tracker = slo_tracker
        self._sla_exporter = sla_exporter

    # ─── 基线管理 ───

    def _baseline(self, axis: Axis, metric: str) -> BehaviorBaseline:
        if metric not in self._baselines[axis]:
            self._baselines[axis][metric] = BehaviorBaseline(
                window_size=100, alpha=self._alpha
            )
        return self._baselines[axis][metric]

    def _direction(self, axis: Axis, metric: str) -> bool:
        """返回 True 表示值越大越退化"""
        return AXIS_METRIC_DIRECTIONS.get(axis, {}).get(metric, True)

    # ─── 公共 API: 记录指标 ───

    def record_quality(self, metric: str, value: float) -> None:
        """记录质量指标 (error_rate / hallucination_count / negative_feedback_rate)"""
        self._record(Axis.QUALITY, metric, value)

    def record_performance(self, metric: str, value: float) -> None:
        """记录性能指标 (p50_latency / p99_latency / throughput)"""
        self._record(Axis.PERFORMANCE, metric, value)

    def record_reliability(self, metric: str, value: float) -> None:
        """记录可靠性指标 (success_rate / retry_rate / timeout_rate)"""
        self._record(Axis.RELIABILITY, metric, value)

    def _record(self, axis: Axis, metric: str, value: int | float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.warning(
                f"DegradationDetector 无效值: axis={axis.value} "
                f"metric={metric} value={value!r}"
            )
            return
        v = float(value)
        baseline = self._baseline(axis, metric)
        baseline.update(v)
        self._latest[axis][metric] = v
        # 同步更新静默偏离计数 (基于新值的 z-score)
        if baseline.ready:
            z = baseline.z_score(v)
            higher_is_worse = self._direction(axis, metric)
            silent_z = (
                (z > self._z_silent and higher_is_worse)
                or (z < -self._z_silent and not higher_is_worse)
            )
            if silent_z:
                self._silent_counters[axis][metric] = (
                    self._silent_counters[axis].get(metric, 0) + 1
                )
            else:
                self._silent_counters[axis][metric] = 0

    def seed_baseline(
        self, axis: Axis, metric: str, mean: float, std: float
    ) -> None:
        """预设基线值 (用于测试 / 快速初始化)

        直接设置 EWMA 基线的 mean / var, 跳过预热阶段。
        不修改对应的 _latest, 后续 record_* 仍会正常更新基线。
        """
        baseline = self._baseline(axis, metric)
        baseline.seed(mean, std, max(self._min_baseline_samples, 10))
        self._latest[axis][metric] = float(mean)
        self._silent_counters[axis][metric] = 0

    # ─── 退化检测 ───

    def check_degradation(self) -> DegradationReport:
        """检查当前退化状态, 返回 DegradationReport"""
        degraded_metrics, silent_metrics, affected_axes, silent_axes = \
            self._collect_metric_deviations()

        severity, axis_label = self._determine_severity(
            affected_axes, silent_axes)

        all_affected = affected_axes | silent_axes
        all_metrics = degraded_metrics + silent_metrics
        silent = bool(silent_metrics)
        recommendations = self._recommendations(all_affected, severity, silent)

        report = DegradationReport(
            axis=axis_label,
            severity=severity,
            affected_axes=sorted(all_affected, key=lambda a: a.value),
            metrics=all_metrics,
            recommendations=recommendations,
            silent=silent,
        )
        self._last_report = report

        # 触发回调与日志 (仅在非 NONE 时)
        if severity != Severity.NONE:
            self._notify_and_log(report, axis_label, severity, silent, all_metrics)
        return report

    def _collect_metric_deviations(self) -> tuple[
        list[MetricDeviation], list[MetricDeviation], set[Axis], set[Axis]
    ]:
        """收集各轴指标的退化与静默退化偏差, 返回
        (degraded_metrics, silent_metrics, affected_axes, silent_axes)"""
        degraded_metrics: list[MetricDeviation] = []
        silent_metrics: list[MetricDeviation] = []
        affected_axes: set[Axis] = set()
        silent_axes: set[Axis] = set()

        for axis in (Axis.QUALITY, Axis.PERFORMANCE, Axis.RELIABILITY):
            for metric, baseline in self._baselines[axis].items():
                if not baseline.ready:
                    continue
                value = self._latest[axis].get(metric, baseline.mean)
                z = baseline.z_score(value)
                higher_is_worse = self._direction(axis, metric)

                # 退化判定: 方向感知, 只在退化方向触发 (>2σ)
                degraded = (
                    (z > self._z_degradation and higher_is_worse)
                    or (z < -self._z_degradation and not higher_is_worse)
                )
                if degraded:
                    degraded_metrics.append(MetricDeviation(
                        name=metric, axis=axis, value=value,
                        baseline_mean=baseline.mean,
                        baseline_std=baseline.std,
                        z_score=z, silent=False,
                    ))
                    affected_axes.add(axis)

                # 静默退化判定: 连续 N 次 >1σ, 且未触发 >2σ 退化
                silent_counter = self._silent_counters[axis].get(metric, 0)
                if (
                    silent_counter >= self._silent_consecutive
                    and not degraded
                ):
                    silent_metrics.append(MetricDeviation(
                        name=metric, axis=axis, value=value,
                        baseline_mean=baseline.mean,
                        baseline_std=baseline.std,
                        z_score=z, silent=True,
                    ))
                    silent_axes.add(axis)

        return degraded_metrics, silent_metrics, affected_axes, silent_axes

    def _determine_severity(self, affected_axes: set[Axis],
                            silent_axes: set[Axis]) -> tuple[Severity, str]:
        """根据受影响轴数量综合判定严重级别 (静默轴也计入受影响轴)"""
        all_affected = affected_axes | silent_axes
        n_axes = len(all_affected)
        if n_axes == 0:
            severity = Severity.NONE
            axis_label = "none"
        elif n_axes == 1:
            severity = Severity.WARNING
            axis_label = "single"
        elif n_axes == 2:
            severity = Severity.CRITICAL
            axis_label = "dual"
        else:
            severity = Severity.EMERGENCY
            axis_label = "triple"
        return severity, axis_label

    def _notify_and_log(self, report: DegradationReport, axis_label: str,
                        severity: Severity, silent: bool,
                        all_metrics: list[MetricDeviation]) -> None:
        """触发回调并记录退化日志 (仅在非 NONE 时调用)"""
        for cb in list(self._callbacks):
            try:
                cb(report)
            except Exception as e:
                logger.error("DegradationDetector 回调异常: {!r}", e)
        log_fn = (
            logger.warning if severity == Severity.WARNING
            else logger.error if severity == Severity.CRITICAL
            else logger.critical
        )
        log_fn(
            f"Degradation.detected axis={axis_label} "
            f"sev={severity.value} "
            f"affected={[a.value for a in report.affected_axes]} "
            f"silent={silent} "
            f"metrics={[m.name for m in all_metrics]}"
        )

    @staticmethod
    def _recommendations(
        axes: set[Axis], severity: Severity, silent: bool
    ) -> list[str]:
        recs: list[str] = []
        if severity == Severity.NONE:
            return recs
        if silent:
            recs.append(
                "静默退化: 无显式错误但指标持续偏离基线, "
                "建议检查 provider 是否切换模型 / 限速 / 上下文污染"
            )
        if Axis.QUALITY in axes:
            recs.append("质量轴退化: 检查错误率 / 幻觉 / 用户反馈, 排查 LLM 提示词")
        if Axis.PERFORMANCE in axes:
            recs.append("性能轴退化: 检查 P50/P99 延迟与吞吐量, 排查上游依赖与资源瓶颈")
        if Axis.RELIABILITY in axes:
            recs.append("可靠性轴退化: 检查成功率 / 重试率 / 超时率, 排查下游故障或限流")
        if severity == Severity.EMERGENCY:
            recs.append("EMERGENCY: 三轴同时退化, 建议进入降级模式并开启熔断")
        elif severity == Severity.CRITICAL:
            recs.append("CRITICAL: 双轴退化, 建议限流并准备降级预案")
        return recs

    # ─── 后台周期检测 ───

    def start(self, interval: int = 60) -> None:
        """启动周期性检测 (后台 asyncio task)

        需在 asyncio 事件循环中调用; 无事件循环时安全降级 (不抛出)。
        """
        if self._task is not None and not self._task.done():
            logger.warning("DegradationDetector 已在运行, 跳过 start")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("DegradationDetector.start 无事件循环, 已跳过后台任务")
            return
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._run(interval))

    async def _run(self, interval: int) -> None:
        if self._stop_event is None:
            raise RuntimeError("_run called without _stop_event")
        try:
            while not self._stop_event.is_set():
                self._pull_from_sources()
                report = self.check_degradation()
                if report.severity != Severity.NONE:
                    logger.info(
                        f"DegradationDetector 周期检测: "
                        f"sev={report.severity.value} axis={report.axis}"
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=interval
                    )
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """停止后台检测"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            self._task = None

    # ─── 复用 SLOTracker / SLAExporter (只读) ───

    def _pull_from_sources(self) -> None:
        """从已注入的指标源拉取最新数据 (只读取, 不修改源)"""
        if self._slo_tracker is not None:
            try:
                self.record_reliability(
                    "success_rate", float(self._slo_tracker.availability())
                )
                self.record_quality(
                    "error_rate", float(self._slo_tracker.error_rate())
                )
                self.record_performance(
                    "p99_latency", float(self._slo_tracker.p99_latency())
                )
                self.record_performance(
                    "throughput", float(self._slo_tracker.throughput())
                )
            except Exception as e:
                logger.debug("SLOTracker 拉取失败: {!r}", e)
        # SLAExporter 暴露的是 Prometheus 风格的累加指标, 无聚合接口, 此处不强行拉取

    # ─── 回调注册 ───

    def on_degradation(self, callback: Callable[[DegradationReport], None]) -> None:
        """注册退化回调 (仅在 severity != NONE 时触发)"""
        self._callbacks.append(callback)

    def clear_callbacks(self) -> None:
        """清空所有已注册的退化回调."""
        self._callbacks.clear()

    # ─── 工具方法 ───

    def reset(self) -> None:
        """重置所有基线与计数 (不影响已注册的回调)"""
        for axis in (Axis.QUALITY, Axis.PERFORMANCE, Axis.RELIABILITY):
            self._baselines[axis].clear()
            self._silent_counters[axis].clear()
            self._latest[axis].clear()
        self._last_report = None

    def stats(self) -> dict:
        """返回检测器统计 (各轴基线数/回调数/运行状态/最近严重级别)."""
        return {
            "baselines": {
                axis.value: len(self._baselines[axis])
                for axis in (Axis.QUALITY, Axis.PERFORMANCE, Axis.RELIABILITY)
            },
            "callbacks": len(self._callbacks),
            "running": self._task is not None and not self._task.done(),
            "last_severity": (
                self._last_report.severity.value
                if self._last_report is not None
                else "none"
            ),
        }


# ============================================================
# 全局单例
# ============================================================

_detector: DegradationDetector | None = None


def get_degradation_detector() -> DegradationDetector:
    """获取全局 DegradationDetector 单例"""
    global _detector
    if _detector is None:
        _detector = DegradationDetector()
    return _detector


def reset_degradation_detector() -> DegradationDetector:
    """重置全局单例 (主要用于测试)"""
    global _detector
    if _detector is not None:
        _detector.stop()
    _detector = DegradationDetector()
    return _detector