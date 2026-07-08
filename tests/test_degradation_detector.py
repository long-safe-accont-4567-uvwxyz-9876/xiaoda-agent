"""Q2: 三轴退化 + 静默退化检测 单元测试.

覆盖场景:
- test_no_degradation           : 正常指标不触发告警
- test_quality_degradation       : 错误率升高触发单轴退化
- test_performance_degradation   : 延迟升高触发单轴退化
- test_reliability_degradation   : 成功率下降触发单轴退化
- test_dual_axis_degradation     : 两轴退化触发 CRITICAL
- test_triple_axis_degradation   : 三轴退化触发 EMERGENCY
- test_silent_degradation        : 静默退化检测 (持续偏离但无错误)
- test_on_degradation_callback   : 退化时回调被触发
- test_start_stop_background     : 后台周期检测能启停
- test_pull_from_slo_tracker     : 从 SLOTracker 拉取数据
"""
import asyncio
import sys
from pathlib import Path


# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.degradation_detector import (
    Axis,
    DegradationDetector,
    DegradationReport,
    MetricDeviation,
    Severity,
    get_degradation_detector,
    reset_degradation_detector,
)
from core.slo_tracker import SLOMeasurement, SLOTarget, SLOTracker


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _seed_all(det: DegradationDetector) -> None:
    """为三轴预设一组稳定的基线, 便于退化检测的精确触发"""
    # Quality
    det.seed_baseline(Axis.QUALITY, "error_rate", mean=0.01, std=0.005)
    det.seed_baseline(Axis.QUALITY, "hallucination_count", mean=0.5, std=0.2)
    det.seed_baseline(Axis.QUALITY, "negative_feedback_rate", mean=0.02, std=0.01)
    # Performance
    det.seed_baseline(Axis.PERFORMANCE, "p50_latency", mean=100.0, std=20.0)
    det.seed_baseline(Axis.PERFORMANCE, "p99_latency", mean=200.0, std=50.0)
    det.seed_baseline(Axis.PERFORMANCE, "throughput", mean=100.0, std=20.0)
    # Reliability
    det.seed_baseline(Axis.RELIABILITY, "success_rate", mean=0.99, std=0.005)
    det.seed_baseline(Axis.RELIABILITY, "retry_rate", mean=0.02, std=0.01)
    det.seed_baseline(Axis.RELIABILITY, "timeout_rate", mean=0.01, std=0.005)


# ────────────────────────────────────────────────────────────
# 1. 正常指标不触发告警
# ────────────────────────────────────────────────────────────

class TestNoDegradation:
    def test_no_degradation_returns_none_severity(self):
        """正常指标不触发告警, severity=NONE, axis=none"""
        det = DegradationDetector()
        _seed_all(det)
        # 推送接近基线的正常值
        det.record_quality("error_rate", 0.012)
        det.record_performance("p99_latency", 210.0)
        det.record_reliability("success_rate", 0.985)

        report = det.check_degradation()

        assert report.severity is Severity.NONE
        assert report.axis == "none"
        assert report.affected_axes == []
        assert report.metrics == []
        assert report.silent is False
        assert report.recommendations == []

    def test_baseline_not_ready_no_degradation(self):
        """基线未 ready (样本不足) 时不触发退化"""
        det = DegradationDetector(min_baseline_samples=10)
        # 只推送少量样本, 基线未 ready
        det.record_quality("error_rate", 0.5)
        report = det.check_degradation()
        assert report.severity is Severity.NONE


# ────────────────────────────────────────────────────────────
# 2-4. 单轴退化
# ────────────────────────────────────────────────────────────

class TestSingleAxisDegradation:
    def test_quality_degradation(self):
        """错误率升高触发单轴退化 (WARNING, quality)"""
        det = DegradationDetector()
        _seed_all(det)
        # 推送一个远超 2σ 的高错误率
        det.record_quality("error_rate", 0.5)

        report = det.check_degradation()

        assert report.severity is Severity.WARNING
        assert report.axis == "single"
        assert Axis.QUALITY in report.affected_axes
        assert Axis.PERFORMANCE not in report.affected_axes
        assert Axis.RELIABILITY not in report.affected_axes
        # 应有 error_rate 的退化记录
        names = [m.name for m in report.metrics]
        assert "error_rate" in names
        err_metric = next(m for m in report.metrics if m.name == "error_rate")
        assert err_metric.silent is False
        assert err_metric.z_score > 2.0
        assert any("质量" in r for r in report.recommendations)

    def test_performance_degradation(self):
        """延迟升高触发单轴退化 (WARNING, performance)"""
        det = DegradationDetector()
        _seed_all(det)
        det.record_performance("p99_latency", 1000.0)

        report = det.check_degradation()

        assert report.severity is Severity.WARNING
        assert report.axis == "single"
        assert Axis.PERFORMANCE in report.affected_axes
        assert Axis.QUALITY not in report.affected_axes
        assert Axis.RELIABILITY not in report.affected_axes
        perf_metric = next(m for m in report.metrics if m.name == "p99_latency")
        assert perf_metric.z_score > 2.0
        assert perf_metric.silent is False
        assert any("性能" in r for r in report.recommendations)

    def test_reliability_degradation(self):
        """成功率下降触发单轴退化 (WARNING, reliability)"""
        det = DegradationDetector()
        _seed_all(det)
        # success_rate 越低越退化: 推送 0.5 (基线 0.99, std 0.005)
        det.record_reliability("success_rate", 0.5)

        report = det.check_degradation()

        assert report.severity is Severity.WARNING
        assert report.axis == "single"
        assert Axis.RELIABILITY in report.affected_axes
        assert Axis.QUALITY not in report.affected_axes
        assert Axis.PERFORMANCE not in report.affected_axes
        rel_metric = next(m for m in report.metrics if m.name == "success_rate")
        # success_rate 是 lower_is_worse, z 应为负且 |z| > 2
        assert rel_metric.z_score < -2.0
        assert rel_metric.silent is False
        assert any("可靠性" in r for r in report.recommendations)

    def test_lower_is_worse_metric_does_not_trigger_on_increase(self):
        """success_rate 上升不应触发退化 (lower_is_worse 方向感知)"""
        det = DegradationDetector()
        _seed_all(det)
        # success_rate 从 0.99 升到 0.9999 (更健康, 不应触发退化)
        det.record_reliability("success_rate", 0.9999)
        report = det.check_degradation()
        # 即便 z 可能 < -1σ (因为方向是负向), 但 success_rate 升高不应被判定为退化
        assert Axis.RELIABILITY not in report.affected_axes


# ────────────────────────────────────────────────────────────
# 5-6. 多轴退化
# ────────────────────────────────────────────────────────────

class TestMultiAxisDegradation:
    def test_dual_axis_degradation(self):
        """两轴同时退化触发 CRITICAL"""
        det = DegradationDetector()
        _seed_all(det)
        # quality + performance 同时退化
        det.record_quality("error_rate", 0.5)
        det.record_performance("p99_latency", 1000.0)

        report = det.check_degradation()

        assert report.severity is Severity.CRITICAL
        assert report.axis == "dual"
        assert Axis.QUALITY in report.affected_axes
        assert Axis.PERFORMANCE in report.affected_axes
        assert Axis.RELIABILITY not in report.affected_axes
        assert len(report.affected_axes) == 2
        assert any("CRITICAL" in r for r in report.recommendations)

    def test_triple_axis_degradation(self):
        """三轴同时退化触发 EMERGENCY"""
        det = DegradationDetector()
        _seed_all(det)
        det.record_quality("error_rate", 0.5)
        det.record_performance("p99_latency", 1000.0)
        det.record_reliability("success_rate", 0.5)

        report = det.check_degradation()

        assert report.severity is Severity.EMERGENCY
        assert report.axis == "triple"
        assert Axis.QUALITY in report.affected_axes
        assert Axis.PERFORMANCE in report.affected_axes
        assert Axis.RELIABILITY in report.affected_axes
        assert len(report.affected_axes) == 3
        assert any("EMERGENCY" in r for r in report.recommendations)
        # to_dict 可序列化
        d = report.to_dict()
        assert d["severity"] == "emergency"
        assert d["axis"] == "triple"
        assert len(d["affected_axes"]) == 3


# ────────────────────────────────────────────────────────────
# 7. 静默退化
# ────────────────────────────────────────────────────────────

class TestSilentDegradation:
    def test_silent_degradation(self):
        """静默退化检测: 持续偏离但未达 2σ 阈值

        seeded p50_latency 基线: mean=100, std=20
        推送 130/140/150 让 z 稳定落在 (1σ, 2σ) 区间, 连续 3 次 >1σ 触发静默退化
        """
        det = DegradationDetector(silent_consecutive=3)
        _seed_all(det)
        for v in [130.0, 140.0, 150.0]:
            det.record_performance("p50_latency", v)

        report = det.check_degradation()

        assert report.severity is Severity.WARNING
        assert report.axis == "single"
        assert Axis.PERFORMANCE in report.affected_axes
        # 静默退化标记为 True
        assert report.silent is True
        # 受影响指标标记为 silent
        silent_metrics = [m for m in report.metrics if m.silent]
        assert len(silent_metrics) >= 1
        assert any(m.name == "p50_latency" for m in silent_metrics)
        # 静默退化的 z-score 应在 (1σ, 2σ) 区间
        p50 = next(m for m in silent_metrics if m.name == "p50_latency")
        assert 1.0 <= abs(p50.z_score) <= 2.5
        # 推荐中应提及静默退化
        assert any("静默" in r for r in report.recommendations)

    def test_silent_counter_resets_on_recovery(self):
        """指标恢复到基线后, 静默计数应清零"""
        det = DegradationDetector(silent_consecutive=5)
        _seed_all(det)
        # 推送 2 次偏离值 (z 约 1.3-1.5, 在 1σ-2σ 之间)
        det.record_performance("p50_latency", 130.0)
        det.record_performance("p50_latency", 140.0)
        assert det._silent_counters[Axis.PERFORMANCE]["p50_latency"] >= 2
        # 推送一个正常值, 计数应清零
        det.record_performance("p50_latency", 100.5)
        assert det._silent_counters[Axis.PERFORMANCE]["p50_latency"] == 0
        report = det.check_degradation()
        assert report.silent is False


# ────────────────────────────────────────────────────────────
# 8. 回调机制
# ────────────────────────────────────────────────────────────

class TestOnDegradationCallback:
    def test_on_degradation_callback_triggered(self):
        """退化时回调被触发"""
        det = DegradationDetector()
        _seed_all(det)
        calls: list[DegradationReport] = []
        det.on_degradation(calls.append)

        det.record_quality("error_rate", 0.5)
        det.check_degradation()

        assert len(calls) == 1
        assert calls[0].severity is Severity.WARNING
        assert Axis.QUALITY in calls[0].affected_axes

    def test_callback_not_triggered_on_no_degradation(self):
        """无退化时不触发回调"""
        det = DegradationDetector()
        _seed_all(det)
        calls = []
        det.on_degradation(calls.append)

        det.record_quality("error_rate", 0.011)
        det.check_degradation()

        assert calls == []

    def test_multiple_callbacks_all_invoked(self):
        """多个回调均被触发"""
        det = DegradationDetector()
        _seed_all(det)
        calls_a, calls_b = [], []
        det.on_degradation(calls_a.append)
        det.on_degradation(calls_b.append)

        det.record_performance("p99_latency", 1000.0)
        det.check_degradation()

        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_callback_exception_isolated(self):
        """单个回调异常不影响其他回调"""
        det = DegradationDetector()
        _seed_all(det)
        good_called = []

        def bad_cb(_):
            raise RuntimeError("callback error")

        def good_cb(r):
            good_called.append(r)

        det.on_degradation(bad_cb)
        det.on_degradation(good_cb)

        # 不应抛出异常
        det.record_reliability("success_rate", 0.5)
        det.check_degradation()

        assert len(good_called) == 1

    def test_clear_callbacks(self):
        """clear_callbacks 后不再触发"""
        det = DegradationDetector()
        _seed_all(det)
        calls = []
        det.on_degradation(calls.append)
        det.clear_callbacks()

        det.record_quality("error_rate", 0.5)
        det.check_degradation()

        assert calls == []


# ────────────────────────────────────────────────────────────
# 9. 后台周期检测 (asyncio)
# ────────────────────────────────────────────────────────────

class TestBackgroundTask:
    def test_start_stop_background_task(self):
        """start/stop 能正常启停后台任务"""
        det = DegradationDetector()

        async def run():
            det.start(interval=1)
            await asyncio.sleep(0.05)
            assert det._task is not None
            assert not det._task.done()
            det.stop()
            await asyncio.sleep(0.05)
            # stop 后任务应已结束或被取消
            assert det._task is None or det._task.done()

        asyncio.run(run())

    def test_start_without_event_loop_does_not_raise(self):
        """无事件循环时 start 不应抛出 (降级处理)"""
        det = DegradationDetector()
        # 在没有运行中的事件循环时调用 start
        det.start(interval=1)
        # 应安全返回, 不抛出
        assert det._task is None
        det.stop()


# ────────────────────────────────────────────────────────────
# 10. 复用 SLOTracker 数据
# ────────────────────────────────────────────────────────────

class TestSLOIntegration:
    def test_pull_from_slo_tracker(self):
        """从 SLOTracker 拉取数据并更新基线"""
        slo = SLOTracker(SLOTarget())
        # 推送若干正常 SLO 测量, 建立基线
        import time as _time
        now = _time.time()
        for _ in range(20):
            slo.record(SLOMeasurement(
                timestamp=now, success=True, latency_ms=120.0
            ))
        det = DegradationDetector(slo_tracker=slo)
        # 首次拉取应不抛异常
        det._pull_from_sources()
        # 基线应有对应指标
        assert "success_rate" in det._baselines[Axis.RELIABILITY]
        assert "error_rate" in det._baselines[Axis.QUALITY]
        assert "p99_latency" in det._baselines[Axis.PERFORMANCE]
        assert "throughput" in det._baselines[Axis.PERFORMANCE]

    def test_slo_degradation_triggers_report(self):
        """SLOTracker 中错误率飙升应触发 quality 退化"""
        slo = SLOTracker(SLOTarget())
        import time as _time
        now = _time.time()
        # 建立健康基线
        for _ in range(50):
            slo.record(SLOMeasurement(
                timestamp=now, success=True, latency_ms=120.0
            ))
        det = DegradationDetector(slo_tracker=slo)
        # 多次拉取以让基线 ready
        for _ in range(15):
            det._pull_from_sources()
        # 推送大量失败让 error_rate 升高
        for _ in range(50):
            slo.record(SLOMeasurement(
                timestamp=now, success=False, latency_ms=120.0
            ))
        det._pull_from_sources()
        report = det.check_degradation()
        # 应检测到 reliability 或 quality 退化
        assert report.severity in (Severity.WARNING, Severity.CRITICAL, Severity.EMERGENCY)


# ────────────────────────────────────────────────────────────
# 11. 全局单例
# ────────────────────────────────────────────────────────────

class TestGlobalSingleton:
    def test_get_degradation_detector_returns_same_instance(self):
        d1 = get_degradation_detector()
        d2 = get_degradation_detector()
        assert d1 is d2

    def test_reset_degradation_detector_creates_new(self):
        old = get_degradation_detector()
        new = reset_degradation_detector()
        assert old is not new
        assert get_degradation_detector() is new


# ────────────────────────────────────────────────────────────
# 12. 数据结构与序列化
# ────────────────────────────────────────────────────────────

class TestDataStructures:
    def test_metric_deviation_fields(self):
        m = MetricDeviation(
            name="error_rate", axis=Axis.QUALITY,
            value=0.5, baseline_mean=0.01, baseline_std=0.005,
            z_score=3.0, silent=False,
        )
        assert m.name == "error_rate"
        assert m.axis is Axis.QUALITY
        assert m.z_score == 3.0
        assert m.silent is False

    def test_report_to_dict_serializable(self):
        det = DegradationDetector()
        _seed_all(det)
        det.record_quality("error_rate", 0.5)
        det.record_performance("p99_latency", 1000.0)
        report = det.check_degradation()
        d = report.to_dict()
        assert isinstance(d, dict)
        assert d["severity"] == "critical"
        assert isinstance(d["affected_axes"], list)
        assert isinstance(d["metrics"], list)
        assert all(isinstance(m, dict) for m in d["metrics"])

    def test_stats(self):
        det = DegradationDetector()
        _seed_all(det)
        det.record_quality("error_rate", 0.011)
        det.check_degradation()
        s = det.stats()
        assert "baselines" in s
        assert "callbacks" in s
        assert "running" in s
        assert "last_severity" in s
        assert s["baselines"]["quality"] == 3
        assert s["baselines"]["performance"] == 3
        assert s["baselines"]["reliability"] == 3

    def test_reset_clears_baselines(self):
        det = DegradationDetector()
        _seed_all(det)
        det.record_quality("error_rate", 0.5)
        det.reset()
        s = det.stats()
        assert s["baselines"]["quality"] == 0
        assert s["baselines"]["performance"] == 0
        assert s["baselines"]["reliability"] == 0
