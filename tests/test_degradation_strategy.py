"""Q4: 4 级降级策略标准化 单元测试.

覆盖场景:
- test_normal_all_features         : L0 时所有功能可用
- test_l1_disables_non_core        : L1 关闭非核心功能 (TTS/表情包)
- test_l2_minimal                  : L2 只保留最小功能 (文本对话)
- test_l3_emergency                : L3 只保留基础响应
- test_recover                     : 恢复到上一级 (L3→L2→L1→L0)
- test_on_level_change_callback    : 级别变化触发回调
- test_trigger_logs_reason         : 触发时记录原因
- test_is_feature_available_unknown: 未知功能默认可用
- test_disabled_features           : 被关闭功能列表正确
- test_evaluate_from_detector      : 集成 DegradationDetector 自动触发
- test_evaluate_from_slo           : 集成 SLOTracker 燃烧率触发
- test_emergency_reply             : 紧急模式固定回复
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.degradation_strategy import (
    DEFAULT_FEATURE_MAP,
    EMERGENCY_FALLBACK_REPLY,
    DegradationLevel,
    DegradationStrategy,
    LevelChangeEvent,
    get_degradation_strategy,
    reset_degradation_strategy,
)

# ────────────────────────────────────────────────────────────
# 1. L0 正常模式: 所有功能可用
# ────────────────────────────────────────────────────────────

class TestNormalLevel:
    def test_normal_all_features(self):
        """L0_NORMAL 时所有声明的功能均可用"""
        strat = DegradationStrategy()
        assert strat.current_level is DegradationLevel.L0_NORMAL
        for feature in DEFAULT_FEATURE_MAP:
            assert strat.is_feature_available(feature), (
                f"L0 应使 {feature} 可用"
            )

    def test_initial_reason_empty(self):
        """初始状态原因为空"""
        strat = DegradationStrategy()
        assert strat.reason == ""

    def test_get_status_l0(self):
        """get_status 在 L0 返回正确结构"""
        strat = DegradationStrategy()
        status = strat.get_status()
        assert status["level"] == "L0_NORMAL"
        assert status["level_value"] == 0
        assert status["disabled_features"] == []
        assert "since" in status
        assert "available_features" in status


# ────────────────────────────────────────────────────────────
# 2. L1 轻度降级: 关闭非核心功能
# ────────────────────────────────────────────────────────────

class TestL1Degraded:
    def test_l1_disables_non_core(self):
        """L1_DEGRADED 关闭 TTS 与表情包, 保留 web/memory/text/basic"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="延迟过高")

        # 非核心功能关闭
        assert not strat.is_feature_available("tts")
        assert not strat.is_feature_available("emotion")
        # 核心功能保留
        assert strat.is_feature_available("web_browse")
        assert strat.is_feature_available("memory_search")
        assert strat.is_feature_available("text_chat")
        assert strat.is_feature_available("basic_response")

    def test_l1_disabled_features_list(self):
        """L1 disabled_features 仅含 tts/emotion"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="test")
        disabled = set(strat.disabled_features())
        assert disabled == {"tts", "emotion"}


# ────────────────────────────────────────────────────────────
# 3. L2 最小化: 只保留文本对话
# ────────────────────────────────────────────────────────────

class TestL2Minimal:
    def test_l2_minimal(self):
        """L2_MINIMAL 只保留 text_chat 与 basic_response"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L2_MINIMAL, reason="资源不足")

        # 增强功能全关
        assert not strat.is_feature_available("tts")
        assert not strat.is_feature_available("emotion")
        assert not strat.is_feature_available("web_browse")
        assert not strat.is_feature_available("memory_search")
        # 最小功能保留
        assert strat.is_feature_available("text_chat")
        assert strat.is_feature_available("basic_response")

    def test_l2_disabled_features_count(self):
        """L2 关闭 4 项功能"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L2_MINIMAL, reason="test")
        assert len(strat.disabled_features()) == 4


# ────────────────────────────────────────────────────────────
# 4. L3 紧急模式: 只保留基础响应
# ────────────────────────────────────────────────────────────

class TestL3Emergency:
    def test_l3_emergency(self):
        """L3_EMERGENCY 只保留 basic_response"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L3_EMERGENCY, reason="系统濒临崩溃")

        # 仅基础响应可用
        assert not strat.is_feature_available("tts")
        assert not strat.is_feature_available("emotion")
        assert not strat.is_feature_available("web_browse")
        assert not strat.is_feature_available("memory_search")
        assert not strat.is_feature_available("text_chat")
        assert strat.is_feature_available("basic_response")

    def test_l3_emergency_reply(self):
        """L3 时 emergency_reply 返回固定模板"""
        strat = DegradationStrategy()
        assert strat.emergency_reply("原始回复") == "原始回复"  # L0 不改写
        strat.trigger(DegradationLevel.L3_EMERGENCY, reason="crash")
        assert strat.emergency_reply("原始回复") == EMERGENCY_FALLBACK_REPLY


# ────────────────────────────────────────────────────────────
# 5. 恢复机制: L3→L2→L1→L0
# ────────────────────────────────────────────────────────────

class TestRecover:
    def test_recover_step_by_step(self):
        """recover() 逐级回升 L3→L2→L1→L0"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L3_EMERGENCY, reason="崩溃")
        assert strat.current_level is DegradationLevel.L3_EMERGENCY

        # L3 → L2
        assert strat.recover() is True
        assert strat.current_level is DegradationLevel.L2_MINIMAL
        # L2 → L1
        assert strat.recover() is True
        assert strat.current_level is DegradationLevel.L1_DEGRADED
        # L1 → L0
        assert strat.recover() is True
        assert strat.current_level is DegradationLevel.L0_NORMAL
        # L0 无法继续恢复
        assert strat.recover() is False
        assert strat.current_level is DegradationLevel.L0_NORMAL

    def test_recover_restores_features(self):
        """恢复后功能重新可用"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L2_MINIMAL, reason="test")
        assert not strat.is_feature_available("tts")
        strat.recover()  # L1
        assert not strat.is_feature_available("tts")  # L1 仍关闭 TTS
        strat.recover()  # L0
        assert strat.is_feature_available("tts")  # L0 恢复


# ────────────────────────────────────────────────────────────
# 6. 级别变化回调
# ────────────────────────────────────────────────────────────

class TestCallback:
    def test_on_level_change_callback(self):
        """级别变化时回调被触发, 携带新旧级别与原因"""
        strat = DegradationStrategy()
        events: list[LevelChangeEvent] = []
        strat.on_level_change(events.append)

        strat.trigger(DegradationLevel.L1_DEGRADED, reason="延迟告警")
        assert len(events) == 1
        ev = events[0]
        assert ev.old_level is DegradationLevel.L0_NORMAL
        assert ev.new_level is DegradationLevel.L1_DEGRADED
        assert ev.reason == "延迟告警"

        strat.recover()
        assert len(events) == 2
        assert events[1].new_level is DegradationLevel.L0_NORMAL

    def test_callback_exception_isolated(self):
        """单个回调异常不影响策略本身与其他回调"""
        strat = DegradationStrategy()
        received: list[LevelChangeEvent] = []

        def bad_cb(ev):
            raise RuntimeError("boom")

        strat.on_level_change(bad_cb)
        strat.on_level_change(received.append)

        # 不应抛出
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="test")
        assert len(received) == 1


# ────────────────────────────────────────────────────────────
# 7. 触发记录原因
# ────────────────────────────────────────────────────────────

class TestTriggerReason:
    def test_trigger_logs_reason(self):
        """trigger 后 reason / since / history 被正确记录"""
        strat = DegradationStrategy()
        before = strat.since
        strat.trigger(DegradationLevel.L2_MINIMAL, reason="内存告警")

        assert strat.reason == "内存告警"
        assert strat.current_level is DegradationLevel.L2_MINIMAL
        # since 更新为触发时刻
        assert strat.since >= before
        # 历史记录
        history = strat.get_history()
        assert len(history) == 1
        assert history[0]["reason"] == "内存告警"
        assert history[0]["new_level"] == "L2_MINIMAL"

    def test_same_level_only_updates_reason(self):
        """同级别 trigger 仅更新原因, 不产生历史事件"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="first")
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="second")
        assert strat.reason == "second"
        # 同级别不产生新的历史事件
        assert len(strat.get_history()) == 1

    def test_get_status_has_reason_and_since(self):
        """get_status 包含 reason / since / disabled_features"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L1_DEGRADED, reason="测试原因")
        status = strat.get_status()
        assert status["reason"] == "测试原因"
        assert isinstance(status["since"], float)
        assert "tts" in status["disabled_features"]


# ────────────────────────────────────────────────────────────
# 8. 未知功能默认可用
# ────────────────────────────────────────────────────────────

class TestUnknownFeature:
    def test_is_feature_available_unknown(self):
        """未在 feature_map 中声明的功能默认可用 (fail-open)"""
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L3_EMERGENCY, reason="test")
        assert strat.is_feature_available("non_existent_feature") is True


# ────────────────────────────────────────────────────────────
# 9. 集成 DegradationDetector 自动触发
# ────────────────────────────────────────────────────────────

class TestDetectorIntegration:
    def test_evaluate_from_detector_critical(self):
        """detector CRITICAL 触发 L1_DEGRADED"""
        from core.degradation_detector import (
            DegradationDetector,
            DegradationReport,
            Severity,
        )
        strat = DegradationStrategy()
        det = DegradationDetector()
        # 构造 CRITICAL 报告
        det._last_report = DegradationReport(
            axis="dual", severity=Severity.CRITICAL,
        )
        event = strat.evaluate_from_detector(det)
        assert strat.current_level is DegradationLevel.L1_DEGRADED
        assert event is not None
        assert event.source == "detector"

    def test_evaluate_from_detector_emergency(self):
        """detector EMERGENCY 触发 L2_MINIMAL"""
        from core.degradation_detector import (
            DegradationDetector,
            DegradationReport,
            Severity,
        )
        strat = DegradationStrategy()
        det = DegradationDetector()
        det._last_report = DegradationReport(
            axis="triple", severity=Severity.EMERGENCY,
        )
        strat.evaluate_from_detector(det)
        assert strat.current_level is DegradationLevel.L2_MINIMAL

    def test_evaluate_from_detector_none_recovers(self):
        """detector NONE 时尝试恢复一级"""
        from core.degradation_detector import (
            DegradationDetector,
            DegradationReport,
            Severity,
        )
        strat = DegradationStrategy()
        strat.trigger(DegradationLevel.L2_MINIMAL, reason="manual")
        det = DegradationDetector()
        det._last_report = DegradationReport(
            axis="none", severity=Severity.NONE,
        )
        strat.evaluate_from_detector(det)
        # 应从 L2 恢复到 L1
        assert strat.current_level is DegradationLevel.L1_DEGRADED

    def test_evaluate_from_detector_no_report(self):
        """detector 无报告时不触发"""
        from core.degradation_detector import DegradationDetector
        strat = DegradationStrategy()
        det = DegradationDetector()  # _last_report 为 None
        event = strat.evaluate_from_detector(det)
        assert event is None
        assert strat.current_level is DegradationLevel.L0_NORMAL


# ────────────────────────────────────────────────────────────
# 10. 集成 SLOTracker 燃烧率触发
# ────────────────────────────────────────────────────────────

class TestSLOIntegration:
    def test_evaluate_from_slo_high_burn(self):
        """SLO burn_rate > 2 触发 L1_DEGRADED"""
        import time as _time

        from core.slo_tracker import SLOMeasurement, SLOTarget, SLOTracker

        tracker = SLOTracker(SLOTarget(availability=0.999))
        # 注入大量失败使 burn_rate 远超 2
        for _ in range(100):
            tracker.record(SLOMeasurement(
                timestamp=_time.time(), success=False, latency_ms=100
            ))
        strat = DegradationStrategy()
        event = strat.evaluate_from_slo(tracker, burn_threshold=2.0)
        assert strat.current_level is DegradationLevel.L1_DEGRADED
        assert event is not None
        assert event.source == "slo_tracker"

    def test_evaluate_from_slo_healthy_no_trigger(self):
        """健康 SLO 不触发降级"""
        import time as _time

        from core.slo_tracker import SLOMeasurement, SLOTarget, SLOTracker

        tracker = SLOTracker(SLOTarget(availability=0.999))
        for _ in range(100):
            tracker.record(SLOMeasurement(
                timestamp=_time.time(), success=True, latency_ms=50
            ))
        strat = DegradationStrategy()
        event = strat.evaluate_from_slo(tracker, burn_threshold=2.0)
        assert event is None
        assert strat.current_level is DegradationLevel.L0_NORMAL


# ────────────────────────────────────────────────────────────
# 11. 全局单例
# ────────────────────────────────────────────────────────────

class TestGlobalSingleton:
    def test_get_and_reset_singleton(self):
        """全局单例获取与重置"""
        s1 = get_degradation_strategy()
        s2 = get_degradation_strategy()
        assert s1 is s2
        # 重置后为全新实例
        s3 = reset_degradation_strategy()
        assert s3 is not s1
        assert s3.current_level is DegradationLevel.L0_NORMAL


# ────────────────────────────────────────────────────────────
# 12. 自动触发接线 (wire_auto_trigger)
# ────────────────────────────────────────────────────────────

class TestWireAutoTrigger:
    def test_wire_auto_trigger_registers_callback(self):
        """wire_auto_trigger 通过 detector.on_degradation 注册回调"""
        from core.degradation_detector import (
            DegradationDetector,
            DegradationReport,
            Severity,
        )
        from core.degradation_strategy import wire_auto_trigger

        reset_degradation_strategy()
        det = DegradationDetector()
        # 构造 EMERGENCY 报告并触发回调
        det._last_report = DegradationReport(
            axis="triple", severity=Severity.EMERGENCY,
        )
        ok = wire_auto_trigger(detector=det, slo_tracker=None)
        assert ok is True
        # 手动触发 detector 回调 (模拟退化检测)
        for cb in det._callbacks:
            cb(det._last_report)
        # 应自动降级到 L2_MINIMAL
        assert get_degradation_strategy().current_level is DegradationLevel.L2_MINIMAL
        reset_degradation_strategy()

