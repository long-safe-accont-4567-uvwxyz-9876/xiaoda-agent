"""TNR 安全自愈规约 (Test-Neutralize-Recover) — Ch3 P2 Chaos Engineering

TNR 三步自愈规约:
- Test:       主动探测异常 (健康检查), 记录故障前健康度
- Neutralize: 中和故障 (降级/重试/隔离故障组件)
- Recover:    恢复正常 (移除故障源, 尝试恢复降级)
- Verify:     验证健康度恢复到故障前水平

"健康度不降" 规约: 自愈后系统健康度应恢复到故障前水平.
若未恢复, 触发告警并保持降级状态 (避免带病运行).

参考:
- core/recovery_orchestrator.py (6 级恢复编排)
- core/degradation_strategy.py  (4 级降级策略)
- core/behavioral_health.py     (5 级健康评分)

用法:
    from chaos.tnr_protocol import TNRProtocol
    from core.behavioral_health import BehavioralHealthScorer
    from core.degradation_strategy import DegradationStrategy
    from core.recovery_orchestrator import RecoveryOrchestrator

    protocol = TNRProtocol(
        health_scorer=BehavioralHealthScorer(),
        recovery_orchestrator=RecoveryOrchestrator(),
        degradation_strategy=DegradationStrategy(),
    )
    report = await protocol.run_protocol("timeout")
    print(report.health_restored, report.to_dict())
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from collections.abc import Callable

from loguru import logger

from core.behavioral_health import BehavioralHealthScorer, HealthScore
from core.degradation_strategy import DegradationLevel, DegradationStrategy
from core.recovery_orchestrator import RecoveryLevel, RecoveryOrchestrator


# ============================================================
# 枚举
# ============================================================

class TNRPhase(str, Enum):
    """TNR 四阶段 (str 子类便于序列化)"""

    TEST = "test"               # 主动探测异常
    NEUTRALIZE = "neutralize"  # 中和故障
    RECOVER = "recover"         # 恢复正常
    VERIFY = "verify"           # 验证健康度


# ============================================================
# 数据结构
# ============================================================

@dataclass
class PhaseResult:
    """单阶段执行结果

    Attributes:
        phase:     阶段枚举
        success:   该阶段是否成功
        details:   阶段详情 (健康度/指标/降级级别等)
        duration:  阶段耗时 (秒)
        timestamp: 时间戳
    """
    phase: TNRPhase
    success: bool
    details: dict = field(default_factory=dict)
    duration: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TNRReport:
    """TNR 协议执行报告

    Attributes:
        fault_type:       故障类型
        phases:           四阶段结果列表
        pre_health:       故障前健康度
        post_health:      自愈后健康度
        health_restored:  健康度是否恢复到故障前水平
        duration:         总耗时 (秒)
        alert_triggered:  是否触发告警 (健康度未恢复时)
    """
    fault_type: str
    phases: list[PhaseResult] = field(default_factory=list)
    pre_health: HealthScore | None = None
    post_health: HealthScore | None = None
    health_restored: bool = False
    duration: float = 0.0
    alert_triggered: bool = False

    def to_dict(self) -> dict:
        """序列化为字典 (便于日志/JSON)"""
        return {
            "fault_type": self.fault_type,
            "phases": [
                {
                    "phase": p.phase.value,
                    "success": p.success,
                    "details": _safe_details(p.details),
                    "duration": round(p.duration, 4),
                }
                for p in self.phases
            ],
            "pre_health_score": self.pre_health.score if self.pre_health else None,
            "pre_health_level": self.pre_health.level.name if self.pre_health else None,
            "post_health_score": self.post_health.score if self.post_health else None,
            "post_health_level": self.post_health.level.name if self.post_health else None,
            "health_restored": self.health_restored,
            "alert_triggered": self.alert_triggered,
            "duration": round(self.duration, 4),
        }

    def phase_result(self, phase: TNRPhase) -> PhaseResult | None:
        """获取指定阶段的结果"""
        for p in self.phases:
            if p.phase == phase:
                return p
        return None


def _safe_details(details: dict) -> dict:
    """将 details 中的 HealthScore 等对象转为可序列化值"""
    safe: dict = {}
    for k, v in details.items():
        if isinstance(v, HealthScore):
            safe[k] = {"score": v.score, "level": v.level.name}
        elif isinstance(v, dict):
            safe[k] = v
        else:
            safe[k] = v
    return safe


# ============================================================
# TNR 协议主类
# ============================================================

# 默认健康指标 (故障前)
_HEALTHY_METRICS: dict = {
    "p50_latency_ms": 500,
    "p99_latency_ms": 800,
    "success_rate": 0.98,
    "error_rate": 0.01,
    "memory_usage": 0.40,
    "tool_success_rate": 0.98,
}

# 各故障类型对应的故障指标 (故障注入后)
_FAULTY_METRICS: dict[str, dict] = {
    "timeout": {
        "p50_latency_ms": 15000,
        "p99_latency_ms": 30000,
        "success_rate": 0.30,
        "error_rate": 0.70,
        "memory_usage": 0.60,
        "tool_success_rate": 0.30,
    },
    "error": {
        "p50_latency_ms": 800,
        "p99_latency_ms": 1200,
        "success_rate": 0.50,
        "error_rate": 0.50,
        "memory_usage": 0.50,
        "tool_success_rate": 0.50,
    },
    "cascading": {
        "p50_latency_ms": 20000,
        "p99_latency_ms": 40000,
        "success_rate": 0.20,
        "error_rate": 0.80,
        "memory_usage": 0.90,
        "tool_success_rate": 0.20,
    },
}

# 默认故障指标 (未在 _FAULTY_METRICS 中时)
_DEFAULT_FAULTY_METRICS: dict = {
    "p50_latency_ms": 10000,
    "p99_latency_ms": 20000,
    "success_rate": 0.50,
    "error_rate": 0.50,
    "memory_usage": 0.70,
    "tool_success_rate": 0.50,
}


class TNRProtocol:
    """TNR 安全自愈规约

    四阶段自愈流程:
    1. TEST:       记录故障前健康度 → 注入故障 → 检测异常
    2. NEUTRALIZE: 触发降级 + 恢复编排 → 中和故障
    3. RECOVER:    移除故障源 → 尝试恢复降级
    4. VERIFY:     验证健康度恢复到故障前水平 (不降)

    自愈失败处理: 若健康度未恢复, 触发告警并保持降级.

    用法:
        protocol = TNRProtocol(scorer, orchestrator, strategy)
        report = await protocol.run_protocol("timeout")
        assert report.health_restored
    """

    def __init__(
        self,
        health_scorer: BehavioralHealthScorer,
        recovery_orchestrator: RecoveryOrchestrator,
        degradation_strategy: DegradationStrategy,
    ) -> None:
        self._health_scorer = health_scorer
        self._recovery_orchestrator = recovery_orchestrator
        self._degradation_strategy = degradation_strategy

        # 内部故障状态 (默认模拟)
        self._fault_active: bool = False
        self._current_fault_type: str | None = None

        # 可注入 hooks (覆盖默认故障注入/移除/指标采集)
        self._inject_fault_fn: Callable[[str], bool] | None = None
        self._remove_fault_fn: Callable[[str], bool] | None = None
        self._collect_metrics_fn: Callable[[], dict] | None = None
        self._probe_fn: Callable | None = None

        # 告警历史
        self._alerts: list[dict] = []

    # ─── hooks 注入 (用于测试/自定义场景) ───

    def set_inject_fault_hook(self, fn: Callable[[str], bool]) -> None:
        """设置故障注入 hook (覆盖默认内部状态切换)"""
        self._inject_fault_fn = fn

    def set_remove_fault_hook(self, fn: Callable[[str], bool]) -> None:
        """设置故障移除 hook"""
        self._remove_fault_fn = fn

    def set_collect_metrics_hook(self, fn: Callable[[], dict]) -> None:
        """设置指标采集 hook (覆盖默认健康/故障指标)"""
        self._collect_metrics_fn = fn

    def set_probe_hook(self, fn: Callable) -> None:
        """设置探针 hook (用于 recovery_orchestrator 的健康探针)"""
        self._probe_fn = fn

    # ─── 状态查询 ───

    @property
    def fault_active(self) -> bool:
        """当前是否有故障注入"""
        return self._fault_active

    def get_alerts(self) -> list[dict]:
        """返回告警历史"""
        return list(self._alerts)

    # ─── 默认实现: 故障注入/移除/指标采集 ───

    def _collect_metrics(self) -> dict:
        """采集当前运行指标 (默认根据故障状态返回)"""
        if self._collect_metrics_fn:
            return self._collect_metrics_fn()
        if self._fault_active:
            return _FAULTY_METRICS.get(
                self._current_fault_type, _DEFAULT_FAULTY_METRICS
            )
        return dict(_HEALTHY_METRICS)

    def _inject_fault(self, fault_type: str) -> bool:
        """注入故障 (默认: 设置内部故障状态)"""
        if self._inject_fault_fn:
            result = self._inject_fault_fn(fault_type)
            # 同步内部状态
            self._fault_active = True
            self._current_fault_type = fault_type
            return result
        self._fault_active = True
        self._current_fault_type = fault_type
        logger.debug(f"TNR.inject_fault type={fault_type}")
        return True

    def _remove_fault(self, fault_type: str) -> bool:
        """移除故障 (默认: 清除内部故障状态)"""
        if self._remove_fault_fn:
            result = self._remove_fault_fn(fault_type)
            self._fault_active = False
            self._current_fault_type = None
            return result
        self._fault_active = False
        self._current_fault_type = None
        logger.debug(f"TNR.remove_fault type={fault_type}")
        return True

    def _detect_anomaly(
        self, pre: HealthScore | None, post: HealthScore | None
    ) -> bool:
        """检测异常: 健康度下降即异常"""
        if pre is None or post is None:
            return False
        return post.score < pre.score

    def _probe(self, **kwargs: Any) -> str:
        """默认健康探针 (供 recovery_orchestrator 调用)

        - 故障未激活: 返回 "ok"
        - 故障激活 + 降级激活: 返回 "degraded_ok" (中和成功)
        - 故障激活 + 未降级: 抛出异常 (未中和)
        """
        if not self._fault_active:
            return "ok"
        if self._degradation_strategy.current_level > DegradationLevel.L0_NORMAL:
            return "degraded_ok"
        raise RuntimeError(
            f"fault '{self._current_fault_type}' not neutralized"
        )

    # ─── 主流程 ───

    async def run_protocol(self, fault_type: str) -> TNRReport:
        """执行 TNR 四阶段自愈规约

        Args:
            fault_type: 故障类型 (timeout / error / cascading / ...)

        Returns:
            TNRReport 包含四阶段结果、故障前后健康度、是否恢复
        """
        t0 = time.time()
        report = TNRReport(fault_type=fault_type)
        logger.info(f"TNR.start fault_type={fault_type}")

        # ── 阶段 1: TEST ──
        # 记录故障前健康度 → 注入故障 → 检测异常
        test_phase = await self._phase_test(fault_type)
        report.phases.append(test_phase)
        report.pre_health = test_phase.details.get("pre_health")

        # ── 阶段 2: NEUTRALIZE ──
        # 触发降级/恢复编排 → 中和故障
        neutralize_phase = await self._phase_neutralize(fault_type)
        report.phases.append(neutralize_phase)

        # ── 阶段 3: RECOVER ──
        # 移除故障 → 尝试恢复正常运行
        recover_phase = await self._phase_recover(fault_type)
        report.phases.append(recover_phase)

        # ── 阶段 4: VERIFY ──
        # 验证健康度恢复到故障前水平
        verify_phase = await self._phase_verify(fault_type, report.pre_health)
        report.phases.append(verify_phase)
        report.post_health = verify_phase.details.get("post_health")
        report.health_restored = verify_phase.details.get("health_restored", False)

        # 自愈失败处理: 健康度未恢复 → 告警并保持降级
        if not report.health_restored:
            self._trigger_alert(fault_type, report.pre_health, report.post_health)
            report.alert_triggered = True
            logger.warning(
                f"TNR.failed health not restored "
                f"(pre={report.pre_health.score if report.pre_health else '?'}, "
                f"post={report.post_health.score if report.post_health else '?'}), "
                f"保持降级并告警"
            )
        else:
            logger.info(
                f"TNR.success health restored "
                f"(pre={report.pre_health.score if report.pre_health else '?'}, "
                f"post={report.post_health.score if report.post_health else '?'})"
            )

        report.duration = time.time() - t0
        logger.info(
            f"TNR.done fault_type={fault_type} "
            f"restored={report.health_restored} "
            f"alert={report.alert_triggered} "
            f"duration={report.duration:.3f}s"
        )
        return report

    # ─── 阶段实现 ───

    async def _phase_test(self, fault_type: str) -> PhaseResult:
        """TEST: 记录故障前健康度 → 注入故障 → 检测异常"""
        t0 = time.time()
        details: dict = {}
        success = True

        # 1. 记录故障前健康度
        pre_metrics = self._collect_metrics()
        pre_health = self._health_scorer.calculate(pre_metrics)
        details["pre_health"] = pre_health
        details["pre_metrics"] = dict(pre_metrics)

        # 2. 注入故障
        try:
            injected = self._inject_fault(fault_type)
            details["fault_injected"] = injected
        except Exception as e:
            details["fault_injected"] = False
            details["inject_error"] = str(e)
            success = False
            injected = False

        # 3. 注入后检测异常
        if injected:
            post_inject_metrics = self._collect_metrics()
            post_inject_health = self._health_scorer.calculate(post_inject_metrics)
            anomaly = self._detect_anomaly(pre_health, post_inject_health)
            details["post_inject_health"] = post_inject_health
            details["anomaly_detected"] = anomaly
            if not anomaly:
                # 注入后未检测到异常, 视为测试未通过
                success = False
        else:
            details["anomaly_detected"] = False

        return PhaseResult(
            phase=TNRPhase.TEST,
            success=success,
            details=details,
            duration=time.time() - t0,
        )

    async def _phase_neutralize(self, fault_type: str) -> PhaseResult:
        """NEUTRALIZE: 触发降级/恢复编排 → 中和故障"""
        t0 = time.time()
        details: dict = {}
        success = True

        # 1. 触发降级 (隔离故障组件, 关闭非核心功能)
        try:
            self._degradation_strategy.trigger(
                DegradationLevel.L1_DEGRADED,
                reason=f"tnr_neutralize: {fault_type}",
                source="tnr_protocol",
            )
            details["degradation_triggered"] = True
            details["degradation_level"] = self._degradation_strategy.current_level.name
        except Exception as e:
            details["degradation_triggered"] = False
            details["degradation_error"] = str(e)
            success = False

        # 2. 恢复编排: 通过探针验证系统在降级下可用
        try:
            probe = self._probe_fn if self._probe_fn else self._probe
            recovery_result = await self._recovery_orchestrator.execute(
                operation=f"tnr_neutralize_{fault_type}",
                handler=probe,
                max_level=RecoveryLevel.FALLBACK,
            )
            details["recovery_success"] = recovery_result.success
            details["recovery_level"] = recovery_result.level_used.name
            details["recovery_attempts"] = recovery_result.attempts
            if not recovery_result.success:
                success = False
        except Exception as e:
            details["recovery_success"] = False
            details["recovery_error"] = str(e)
            success = False

        return PhaseResult(
            phase=TNRPhase.NEUTRALIZE,
            success=success,
            details=details,
            duration=time.time() - t0,
        )

    async def _phase_recover(self, fault_type: str) -> PhaseResult:
        """RECOVER: 移除故障 → 尝试恢复正常运行"""
        t0 = time.time()
        details: dict = {}
        success = True

        # 1. 移除故障源
        try:
            removed = self._remove_fault(fault_type)
            details["fault_removed"] = removed
            if not removed:
                success = False
        except Exception as e:
            details["fault_removed"] = False
            details["remove_error"] = str(e)
            success = False
            removed = False

        # 2. 尝试恢复降级 (逐级回升 L1 → L0)
        if removed:
            try:
                recovered = self._degradation_strategy.recover(source="tnr_protocol")
                details["degradation_recovered"] = recovered
                details["degradation_level"] = (
                    self._degradation_strategy.current_level.name
                )
            except Exception as e:
                details["degradation_recovered"] = False
                details["degradation_recover_error"] = str(e)

        return PhaseResult(
            phase=TNRPhase.RECOVER,
            success=success,
            details=details,
            duration=time.time() - t0,
        )

    async def _phase_verify(
        self, fault_type: str, pre_health: HealthScore | None
    ) -> PhaseResult:
        """VERIFY: 验证健康度恢复到故障前水平"""
        t0 = time.time()
        details: dict = {}
        success = True

        # 采集自愈后指标并计算健康度
        post_metrics = self._collect_metrics()
        post_health = self._health_scorer.calculate(post_metrics)
        details["post_health"] = post_health
        details["post_metrics"] = dict(post_metrics)

        # 健康度恢复检查: post >= pre (健康度不降)
        if pre_health is not None and post_health is not None:
            health_restored = post_health.score >= pre_health.score
            details["health_restored"] = health_restored
            details["pre_score"] = pre_health.score
            details["post_score"] = post_health.score
            if not health_restored:
                success = False
        else:
            details["health_restored"] = False
            success = False

        return PhaseResult(
            phase=TNRPhase.VERIFY,
            success=success,
            details=details,
            duration=time.time() - t0,
        )

    # ─── 告警 ───

    def _trigger_alert(
        self,
        fault_type: str,
        pre_health: HealthScore | None,
        post_health: HealthScore | None,
    ) -> None:
        """触发告警 (健康度未恢复) 并保持降级状态

        - 记录告警到 _alerts 历史
        - 若当前已恢复到 L0, 主动触发 L1 保持降级 (避免带病运行)
        """
        pre_score = pre_health.score if pre_health else None
        post_score = post_health.score if post_health else None
        alert = {
            "fault_type": fault_type,
            "pre_score": pre_score,
            "post_score": post_score,
            "timestamp": time.time(),
            "message": (
                f"TNR 自愈失败: {fault_type} 故障后健康度未恢复 "
                f"(pre={pre_score}, post={post_score}), 保持降级"
            ),
        }
        self._alerts.append(alert)
        logger.warning(f"TNR.alert: {alert['message']}")

        # 保持降级: 若当前已恢复到 L0, 主动降级到 L1
        if self._degradation_strategy.current_level <= DegradationLevel.L0_NORMAL:
            self._degradation_strategy.trigger(
                DegradationLevel.L1_DEGRADED,
                reason=f"tnr_alert: health not restored after {fault_type}",
                source="tnr_protocol",
            )
