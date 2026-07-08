"""TNR 安全自愈规约 单元测试 — Ch3 P2 Chaos Engineering

测试覆盖:
- test_tnr_full_flow:                  完整 TNR 流程 (四阶段执行)
- test_health_restored:                自愈后健康度恢复到故障前水平
- test_neutralize_triggers_degradation: 中和阶段触发降级
- test_recover_removes_fault:          恢复阶段移除故障
- test_verify_checks_health:           验证阶段检查健康度
- test_failed_recovery_alerts:         恢复失败时触发告警并保持降级
"""
import asyncio
import sys
from pathlib import Path


# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chaos.tnr_protocol import (
    TNRPhase,
    TNRProtocol,
    TNRReport,
    PhaseResult,
)
from chaos.tnr_scenarios import (
    DEFAULT_SCENARIOS,
    make_scenario,
    run_scenario,
)
from core.behavioral_health import BehavioralHealthScorer, HealthLevel
from core.degradation_strategy import (
    DegradationLevel,
    reset_degradation_strategy,
)
from core.recovery_orchestrator import RecoveryOrchestrator


# ────────────────────────────────────────────────────────────
# 辅助: 构建 TNRProtocol 实例
# ────────────────────────────────────────────────────────────

def _make_protocol(
    fault_type: str = "timeout",
) -> TNRProtocol:
    """构建带默认指标的 TNRProtocol (使用真实组件)"""
    protocol = TNRProtocol(
        health_scorer=BehavioralHealthScorer(),
        recovery_orchestrator=RecoveryOrchestrator(),
        degradation_strategy=reset_degradation_strategy(),
    )
    return protocol


# ────────────────────────────────────────────────────────────
# 1. 完整 TNR 流程: 四阶段执行
# ────────────────────────────────────────────────────────────

class TestTNRFullFlow:
    def test_tnr_full_flow(self):
        """完整 TNR 流程: 四阶段均执行, 报告字段完整"""
        protocol = _make_protocol()
        report = asyncio.run(protocol.run_protocol("timeout"))

        # 报告类型
        assert isinstance(report, TNRReport)
        assert report.fault_type == "timeout"

        # 四阶段全部执行
        assert len(report.phases) == 4
        phase_enums = [p.phase for p in report.phases]
        assert phase_enums == [
            TNRPhase.TEST,
            TNRPhase.NEUTRALIZE,
            TNRPhase.RECOVER,
            TNRPhase.VERIFY,
        ]

        # 每个阶段都有 duration 和 details
        for phase_result in report.phases:
            assert isinstance(phase_result, PhaseResult)
            assert phase_result.duration >= 0
            assert isinstance(phase_result.details, dict)

        # 故障前/后健康度已记录
        assert report.pre_health is not None
        assert report.post_health is not None

        # 总耗时
        assert report.duration > 0

    def test_tnr_all_scenarios(self):
        """三类场景均可完整运行"""
        for name in DEFAULT_SCENARIOS:
            report = asyncio.run(run_scenario(name))
            assert isinstance(report, TNRReport)
            assert len(report.phases) == 4


# ────────────────────────────────────────────────────────────
# 2. 自愈后健康度恢复
# ────────────────────────────────────────────────────────────

class TestHealthRestored:
    def test_health_restored(self):
        """自愈后健康度恢复到故障前水平 (健康度不降)"""
        protocol = _make_protocol()
        report = asyncio.run(protocol.run_protocol("error"))

        # 故障前健康度应为 EXCELLENT (5)
        assert report.pre_health.score == 5
        assert report.pre_health.level == HealthLevel.EXCELLENT

        # 自愈后健康度恢复
        assert report.health_restored is True
        assert report.post_health.score >= report.pre_health.score
        assert report.post_health.level == HealthLevel.EXCELLENT

        # 未触发告警
        assert report.alert_triggered is False
        assert len(protocol.get_alerts()) == 0


# ────────────────────────────────────────────────────────────
# 3. 中和阶段触发降级
# ────────────────────────────────────────────────────────────

class TestNeutralizeTriggersDegradation:
    def test_neutralize_triggers_degradation(self):
        """NEUTRALIZE 阶段触发降级 (L1_DEGRADED)"""
        protocol = _make_protocol()
        degradation = protocol._degradation_strategy

        # 初始 L0
        assert degradation.current_level == DegradationLevel.L0_NORMAL

        report = asyncio.run(protocol.run_protocol("timeout"))

        # 找到 NEUTRALIZE 阶段
        neutralize = report.phase_result(TNRPhase.NEUTRALIZE)
        assert neutralize is not None

        # 降级被触发
        assert neutralize.details.get("degradation_triggered") is True
        assert neutralize.details.get("degradation_level") == "L1_DEGRADED"

        # recovery_orchestrator 被调用 (探针在降级下成功)
        assert neutralize.details.get("recovery_success") is True
        assert neutralize.success is True


# ────────────────────────────────────────────────────────────
# 4. 恢复阶段移除故障
# ────────────────────────────────────────────────────────────

class TestRecoverRemovesFault:
    def test_recover_removes_fault(self):
        """RECOVER 阶段移除故障源"""
        protocol = _make_protocol()

        report = asyncio.run(protocol.run_protocol("timeout"))

        # 找到 RECOVER 阶段
        recover = report.phase_result(TNRPhase.RECOVER)
        assert recover is not None

        # 故障已移除
        assert recover.details.get("fault_removed") is True
        assert recover.success is True

        # 内部故障状态已清除
        assert protocol.fault_active is False

        # 降级已恢复 (L1 → L0)
        assert recover.details.get("degradation_recovered") in (True, False)
        # 最终降级级别应为 L0 (自愈成功时)
        assert protocol._degradation_strategy.current_level == DegradationLevel.L0_NORMAL

    def test_recover_via_scenarios(self):
        """通过场景验证 RECOVER 阶段"""
        for name in DEFAULT_SCENARIOS:
            protocol = make_scenario(name)
            report = asyncio.run(protocol.run_protocol(
                {"tnr_timeout": "timeout",
                 "tnr_error": "error",
                 "tnr_cascading": "cascading"}[name]
            ))
            recover = report.phase_result(TNRPhase.RECOVER)
            assert recover.details.get("fault_removed") is True
            assert protocol.fault_active is False


# ────────────────────────────────────────────────────────────
# 5. 验证阶段检查健康度
# ────────────────────────────────────────────────────────────

class TestVerifyChecksHealth:
    def test_verify_checks_health(self):
        """VERIFY 阶段计算并检查自愈后健康度"""
        protocol = _make_protocol()
        report = asyncio.run(protocol.run_protocol("timeout"))

        verify = report.phase_result(TNRPhase.VERIFY)
        assert verify is not None

        # post_health 已计算
        assert "post_health" in verify.details
        post_health = verify.details["post_health"]
        assert post_health is not None
        assert post_health.score > 0

        # health_restored 标记已设置
        assert "health_restored" in verify.details
        assert "pre_score" in verify.details
        assert "post_score" in verify.details

        # 自愈成功: post >= pre
        assert verify.details["health_restored"] is True
        assert verify.details["post_score"] >= verify.details["pre_score"]
        assert verify.success is True


# ────────────────────────────────────────────────────────────
# 6. 恢复失败时告警
# ────────────────────────────────────────────────────────────

class TestFailedRecoveryAlerts:
    def test_failed_recovery_alerts(self):
        """恢复失败 (健康度未恢复) 时触发告警并保持降级"""

        # 用自定义指标 hook 模拟健康度未恢复:
        # 移除故障后指标仍然差 (模拟永久性损伤)
        call_count = {"n": 0}

        def faulty_metrics() -> dict:
            call_count["n"] += 1
            # 前几次 (TEST 阶段: pre + post_inject) 返回正常/故障指标
            # 最后一次 (VERIFY 阶段) 返回差指标 → 健康度未恢复
            # 通过 fault_active 状态判断
            if protocol.fault_active:
                # 故障注入后: 返回故障指标
                return {
                    "p50_latency_ms": 15000,
                    "p99_latency_ms": 30000,
                    "success_rate": 0.30,
                    "error_rate": 0.70,
                    "memory_usage": 0.60,
                    "tool_success_rate": 0.30,
                }
            # 故障前: 正常指标
            # 故障移除后 (VERIFY): 仍然返回差指标 (模拟未恢复)
            # 用 call_count 区分: 第 1 次是 pre_health (正常), 后续看状态
            if call_count["n"] == 1:
                # 第一次采集 = 故障前健康度
                return {
                    "p50_latency_ms": 500,
                    "p99_latency_ms": 800,
                    "success_rate": 0.98,
                    "error_rate": 0.01,
                    "memory_usage": 0.40,
                    "tool_success_rate": 0.98,
                }
            # 故障移除后 (VERIFY): 返回差指标 (健康度未恢复)
            return {
                "p50_latency_ms": 8000,
                "p99_latency_ms": 15000,
                "success_rate": 0.60,
                "error_rate": 0.40,
                "memory_usage": 0.80,
                "tool_success_rate": 0.60,
            }

        protocol = _make_protocol()
        protocol.set_collect_metrics_hook(faulty_metrics)

        report = asyncio.run(protocol.run_protocol("timeout"))

        # 健康度未恢复
        assert report.health_restored is False
        assert report.post_health.score < report.pre_health.score

        # 触发告警
        assert report.alert_triggered is True
        alerts = protocol.get_alerts()
        assert len(alerts) >= 1
        assert "timeout" in alerts[0]["message"]

        # VERIFY 阶段失败
        verify = report.phase_result(TNRPhase.VERIFY)
        assert verify.success is False
        assert verify.details.get("health_restored") is False

        # 保持降级: 当前级别应为 L1 (未恢复到 L0)
        assert protocol._degradation_strategy.current_level >= DegradationLevel.L1_DEGRADED

    def test_failed_recovery_alert_history(self):
        """多次失败场景告警历史累积"""

        # 用 call_count 区分 pre_health (正常) 和 post_health (未恢复)
        call_count = {"n": 0}

        def metrics_fn() -> dict:
            call_count["n"] += 1
            if protocol.fault_active:
                # 故障注入后: 返回故障指标
                return {
                    "p50_latency_ms": 15000,
                    "p99_latency_ms": 30000,
                    "success_rate": 0.30,
                    "error_rate": 0.70,
                    "memory_usage": 0.60,
                    "tool_success_rate": 0.30,
                }
            # 第一次采集 = 故障前健康度 (正常)
            if call_count["n"] == 1:
                return {
                    "p50_latency_ms": 500,
                    "p99_latency_ms": 800,
                    "success_rate": 0.98,
                    "error_rate": 0.01,
                    "memory_usage": 0.40,
                    "tool_success_rate": 0.98,
                }
            # 故障移除后 (VERIFY): 返回差指标 (健康度未恢复)
            return {
                "p50_latency_ms": 8000,
                "p99_latency_ms": 15000,
                "success_rate": 0.60,
                "error_rate": 0.40,
                "memory_usage": 0.80,
                "tool_success_rate": 0.60,
            }

        protocol = _make_protocol()
        protocol.set_collect_metrics_hook(metrics_fn)

        asyncio.run(protocol.run_protocol("error"))
        # 第二次 run_protocol: call_count 继续, 但第一次采集是 pre_health
        # 需要重置 call_count 使第一次采集为正常指标
        call_count["n"] = 0
        asyncio.run(protocol.run_protocol("timeout"))

        alerts = protocol.get_alerts()
        assert len(alerts) == 2
        assert alerts[0]["fault_type"] == "error"
        assert alerts[1]["fault_type"] == "timeout"
