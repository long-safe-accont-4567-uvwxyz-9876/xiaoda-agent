"""ReliabilityBench — 三维可靠性基准测试 (Ch2 P1 Chaos Engineering)

参考:
- tests/fault_injection.py (FaultInjectingLLMClient 故障注入客户端)
- core/recovery_orchestrator.py (6 级恢复编排)
- core/degradation_strategy.py (4 级降级策略)
- Google SRE: Three-axis reliability (容错 / 恢复速度 / 降级优雅度)

三维可靠性评分:
- Fault Tolerance (容错): 故障时能否继续运行 = 故障恢复率 (恢复/注入)
- Recovery Speed (恢复速度): 故障后多久恢复正常 = 1 / avg_recovery_time (秒)
- Degradation Gracefulness (降级优雅度): 降级时用户体验是否平滑
    = 降级触发率 * (1 - 用户感知中断率)

内置 7 个测试场景:
- single_timeout     : 单次超时 → 验证重试
- burst_errors       : 连续 5 次错误 → 验证降级触发
- slow_response      : 10s 慢响应 → 验证超时处理
- partial_failure    : 50% 故障率 → 验证混合降级
- cascading_failure  : 多组件故障 → 验证级联恢复
- recovery_test      : 故障后恢复 → 验证恢复时间
- sustained_load     : 持续负载 100 请求 → 验证稳定性

用法:
    bench = ReliabilityBench(agent=my_agent, fault_client=fault_client)
    report = await bench.run_suite()
    print(report.overall_score, report.three_axis_scores)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# 从 chaos 内部导入，断开对 tests/ 的生产依赖
from chaos._fault_types import (
    FaultConfig,
    SimpleFaultInjectingLLMClient,
    FaultType,
)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class ScenarioResult:
    """单个场景的测试结果

    Attributes:
        name: 场景名 (single_timeout / burst_errors / ...)
        passed: 是否通过 (达到该场景验收条件)
        duration: 场景总耗时 (秒)
        faults_injected: 实际注入的故障数
        faults_recovered: 通过重试/降级恢复的故障数
        degradation_triggered: 是否触发了降级路径
        recovery_time: 平均恢复时间 (秒) — 用于恢复速度评分
        user_perceived_interrupt: 是否发生用户感知中断 (未降级且未恢复)
        details: 额外信息 (请求数、故障率等)
    """
    name: str
    passed: bool
    duration: float = 0.0
    faults_injected: int = 0
    faults_recovered: int = 0
    degradation_triggered: bool = False
    recovery_time: float = 0.0
    user_perceived_interrupt: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class BenchReport:
    """基准测试报告"""
    scenario_results: list[ScenarioResult] = field(default_factory=list)
    overall_score: float = 0.0  # 0-100
    three_axis_scores: dict = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """将基准测试报告序列化为字典 (含各场景结果与三维评分)."""
        return {
            "scenario_results": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "duration": round(s.duration, 4),
                    "faults_injected": s.faults_injected,
                    "faults_recovered": s.faults_recovered,
                    "degradation_triggered": s.degradation_triggered,
                    "recovery_time": round(s.recovery_time, 4),
                    "user_perceived_interrupt": s.user_perceived_interrupt,
                    "details": s.details,
                }
                for s in self.scenario_results
            ],
            "overall_score": self.overall_score,
            "three_axis_scores": dict(self.three_axis_scores),
            "recommendations": list(self.recommendations),
        }


# ============================================================
# 内置场景列表 (顺序即默认运行顺序)
# ============================================================

DEFAULT_SCENARIOS: list[str] = [
    "single_timeout",
    "burst_errors",
    "slow_response",
    "partial_failure",
    "cascading_failure",
    "recovery_test",
    "sustained_load",
]


# ============================================================
# ReliabilityBench 主类
# ============================================================

class ReliabilityBench:
    """三维可靠性基准测试套件

    用法:
        bench = ReliabilityBench(agent=my_agent, fault_client=fault_client)
        report = await bench.run_suite()
        print(report.overall_score, report.three_axis_scores)

    可配置参数 (用于在测试中固定随机行为 / 加速):
        single_timeout_prob        : single_timeout 故障注入概率 (默认 0.3)
        single_timeout_requests   : single_timeout 请求数 (默认 10)
        burst_errors_count        : burst_errors 请求数 (默认 5)
        slow_response_ms          : slow_response 模拟延迟 (默认 100ms)
        partial_failure_prob      : partial_failure 故障率 (默认 0.5)
        partial_failure_requests  : partial_failure 请求数 (默认 20)
        cascading_failure_prob    : cascading_failure 每类故障概率 (默认 0.3)
        cascading_failure_requests: cascading_failure 请求数 (默认 15)
        recovery_test_fault_count : recovery_test 故障阶段请求数 (默认 3)
        sustained_load_prob       : sustained_load 故障率 (默认 0.1)
        sustained_load_requests   : sustained_load 请求数 (默认 100)
    """

    def __init__(
        self,
        agent: Any,
        fault_client: SimpleFaultInjectingLLMClient,
        # 场景参数 (测试中可覆盖以加速/确定性)
        single_timeout_prob: float = 0.3,
        single_timeout_requests: int = 10,
        burst_errors_count: int = 5,
        slow_response_ms: int = 100,
        partial_failure_prob: float = 0.5,
        partial_failure_requests: int = 20,
        cascading_failure_prob: float = 0.3,
        cascading_failure_requests: int = 15,
        recovery_test_fault_count: int = 3,
        sustained_load_prob: float = 0.1,
        sustained_load_requests: int = 100,
    ) -> None:
        self.agent = agent
        self.fault_client = fault_client
        # 场景参数
        self.single_timeout_prob = single_timeout_prob
        self.single_timeout_requests = single_timeout_requests
        self.burst_errors_count = burst_errors_count
        self.slow_response_ms = slow_response_ms
        self.partial_failure_prob = partial_failure_prob
        self.partial_failure_requests = partial_failure_requests
        self.cascading_failure_prob = cascading_failure_prob
        self.cascading_failure_requests = cascading_failure_requests
        self.recovery_test_fault_count = recovery_test_fault_count
        self.sustained_load_prob = sustained_load_prob
        self.sustained_load_requests = sustained_load_requests

        self._scenarios = {
            "single_timeout": self._scenario_single_timeout,
            "burst_errors": self._scenario_burst_errors,
            "slow_response": self._scenario_slow_response,
            "partial_failure": self._scenario_partial_failure,
            "cascading_failure": self._scenario_cascading_failure,
            "recovery_test": self._scenario_recovery_test,
            "sustained_load": self._scenario_sustained_load,
        }

    # ─── 公共入口 ───

    async def run_suite(
        self, scenarios: list[str] | None = None
    ) -> BenchReport:
        """运行测试套件

        Args:
            scenarios: 场景列表 (None 则运行所有内置场景)
        Returns:
            BenchReport (含 scenario_results / overall_score / three_axis_scores / recommendations)
        """
        names = scenarios if scenarios is not None else list(DEFAULT_SCENARIOS)
        results: list[ScenarioResult] = []
        for name in names:
            handler = self._scenarios.get(name)
            if handler is None:
                logger.warning(f"ReliabilityBench: 未知场景 {name}, 跳过")
                continue
            try:
                result = await handler()
            except Exception as e:
                logger.error(f"ReliabilityBench 场景 {name} 异常: {e!r}")
                result = ScenarioResult(
                    name=name, passed=False,
                    details={"error": f"{type(e).__name__}: {e}"},
                )
            results.append(result)
            logger.info(
                f"Bench.scenario {name}: passed={result.passed} "
                f"faults={result.faults_injected}/{result.faults_recovered} "
                f"degradation={'Y' if result.degradation_triggered else 'N'} "
                f"recovery_time={result.recovery_time:.3f}s"
            )

        report = BenchReport(scenario_results=results)
        self.compute_scores(report)
        self.generate_recommendations(report)
        logger.info(
            f"ReliabilityBench 套件完成: overall={report.overall_score:.1f} "
            f"FT={report.three_axis_scores.get('fault_tolerance', 0):.1f} "
            f"RS={report.three_axis_scores.get('recovery_speed', 0):.1f} "
            f"DG={report.three_axis_scores.get('degradation_gracefulness', 0):.1f}"
        )
        return report

    # ─── 评分计算 (公开以便测试) ───

    def compute_scores(self, report: BenchReport) -> None:
        """计算三维评分与综合评分 (写入 report.three_axis_scores / overall_score)

        评分公式:
        - fault_tolerance = faults_recovered / faults_injected (无故障视为 1.0)
        - recovery_speed  = 1 / avg_recovery_time (秒); 用 1/(1+t) 归一化到 [0,1]
        - degradation_gracefulness = 降级触发率 * (1 - 用户感知中断率)
        - overall = (FT*0.4 + RS*0.3 + DG*0.3) * 100
        """
        total_injected = sum(r.faults_injected for r in report.scenario_results)
        total_recovered = sum(r.faults_recovered for r in report.scenario_results)

        # 容错: 故障恢复率 (恢复/注入)
        fault_tolerance = total_recovered / total_injected if total_injected > 0 else 1.0

        # 恢复速度: 1 / avg_recovery_time (秒)
        recovery_times = [
            r.recovery_time for r in report.scenario_results
            if r.faults_injected > 0 and r.recovery_time > 0
        ]
        if recovery_times:
            avg_rt = sum(recovery_times) / len(recovery_times)
            # 用 1/(1+t) 归一化, t=0 时满速 1.0
            recovery_speed = 1.0 / (1.0 + avg_rt)
        else:
            # 无故障发生, 视为满速恢复
            recovery_speed = 1.0

        # 降级优雅度: 降级触发率 * (1 - 用户感知中断率)
        fault_scenarios = [
            r for r in report.scenario_results if r.faults_injected > 0
        ]
        if fault_scenarios:
            triggered = sum(1 for r in fault_scenarios if r.degradation_triggered)
            degradation_rate = triggered / len(fault_scenarios)
            interrupt_count = sum(
                1 for r in fault_scenarios if r.user_perceived_interrupt
            )
            interrupt_rate = interrupt_count / len(fault_scenarios)
        else:
            # 无故障场景: 视为完美降级 (无需降级即优雅)
            degradation_rate = 1.0
            interrupt_rate = 0.0
        gracefulness = degradation_rate * (1.0 - interrupt_rate)

        # 综合评分 (加权平均)
        overall = (
            fault_tolerance * 0.4
            + recovery_speed * 0.3
            + gracefulness * 0.3
        ) * 100.0

        report.three_axis_scores = {
            "fault_tolerance": round(fault_tolerance * 100, 2),
            "recovery_speed": round(recovery_speed * 100, 2),
            "degradation_gracefulness": round(gracefulness * 100, 2),
        }
        report.overall_score = round(overall, 2)

    def generate_recommendations(self, report: BenchReport) -> None:
        """根据评分生成改进建议 (写入 report.recommendations)"""
        recs: list[str] = []
        scores = report.three_axis_scores
        ft = scores.get("fault_tolerance", 0.0)
        rs = scores.get("recovery_speed", 0.0)
        dg = scores.get("degradation_gracefulness", 0.0)

        if ft < 60.0:
            recs.append(
                f"容错性偏低 ({ft:.0f}/100): 建议增加重试次数与降级 fallback, "
                f"确保注入的故障都能被恢复"
            )
        elif ft < 80.0:
            recs.append(
                f"容错性中等 ({ft:.0f}/100): 部分故障未恢复, "
                f"检查异常路径是否完整捕获"
            )

        if rs < 60.0:
            recs.append(
                f"恢复速度偏慢 ({rs:.0f}/100): 平均恢复时间过长, "
                f"建议缩短指数退避间隔或提前触发降级"
            )
        elif rs < 80.0:
            recs.append(
                f"恢复速度中等 ({rs:.0f}/100): 仍有优化空间, "
                f"考虑预热线程池/缓存以加速恢复"
            )

        if dg < 60.0:
            recs.append(
                f"降级优雅度不足 ({dg:.0f}/100): 用户感知中断较多, "
                f"建议在降级路径提供默认兜底回复"
            )
        elif dg < 80.0:
            recs.append(
                f"降级优雅度中等 ({dg:.0f}/100): 部分场景未触发降级, "
                f"建议降低 DegradationDetector 阈值"
            )

        # 失败场景逐项提示
        for r in report.scenario_results:
            if not r.passed:
                recs.append(
                    f"场景 {r.name} 未通过: 注入 {r.faults_injected} 故障, "
                    f"恢复 {r.faults_recovered}"
                )

        if not recs:
            recs.append("三维评分均 >= 80, 可靠性表现良好, 建议保持现有配置")
        report.recommendations = recs

    # ─── 内部辅助: 调用 agent ───

    async def _call_agent(self, prompt: str = "hello") -> Any:
        """通过 fault_client 调用 agent

        Returns:
            (response, was_fault, degradation_triggered)
            - response: dict 响应
            - was_fault: 是否发生了故障 (TIMEOUT/RATE_LIMIT/EMPTY 等)
            - degradation_triggered: 是否通过降级路径恢复 (而非真实响应)
        Raises:
            Exception: 故障未被降级处理时重新抛出
        """
        messages = [{"role": "user", "content": prompt}]
        try:
            resp = await self.fault_client.complete(messages)
        except Exception as e:
            # 故障以异常形式抛出 (TIMEOUT 等)
            return self._try_degraded_reply(str(e))

        # 检查响应是否为故障
        is_fault, fault_reason = self._detect_fault(resp)
        if not is_fault:
            return (resp, False, False)

        # 故障响应, 尝试降级
        return self._try_degraded_reply(fault_reason, original_resp=resp)

    def _detect_fault(self, resp: Any) -> tuple[bool, str]:
        """检测响应是否为故障响应"""
        if not isinstance(resp, dict):
            return (False, "")
        if resp.get("error"):
            return (True, str(resp.get("error")))
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return (True, "malformed_response")
        if not content:
            return (True, "empty_response")
        if content == "{invalid json}":
            return (True, "invalid_json")
        if content == "这是一个不完整的":
            return (True, "partial_response")
        return (False, "")

    def _try_degraded_reply(self, reason: str, original_resp: dict | None = None) -> tuple:
        """尝试通过 agent.degraded_reply 降级

        Returns:
            (response, was_fault=True, degradation_triggered)
        Raises:
            RuntimeError: agent 无 degraded_reply 或返回空
        """
        degraded_reply = getattr(self.agent, "degraded_reply", None)
        if callable(degraded_reply):
            try:
                fallback = degraded_reply(reason)
            except Exception as e:
                logger.debug(f"degraded_reply 异常: {e!r}")
                fallback = None
            if fallback:
                return (
                    {
                        "choices": [{"message": {"content": str(fallback)}}],
                        "_degraded": True,
                    },
                    True,
                    True,
                )
        # 未降级, 抛出 (上层场景计入 user_interrupt)
        raise RuntimeError(f"agent 未提供降级回复 (reason={reason})")

    def _backup_faults(self) -> list[FaultConfig]:
        """备份当前 fault_client 的 faults 配置"""
        return list(self.fault_client._faults)

    def _restore_faults(self, backup: list[FaultConfig]) -> None:
        """恢复 fault_client 的 faults 配置"""
        self.fault_client._faults = list(backup)

    # ─── 内置场景 ───

    async def _scenario_single_timeout(self) -> ScenarioResult:
        """场景 1: 单次超时 → 验证重试

        注入 30% 概率超时, 跑 N 次请求, 验证 agent 能通过降级恢复
        """
        t0 = time.time()
        backup = self._backup_faults()
        self.fault_client._faults.clear()
        self.fault_client.add_fault(
            FaultConfig(FaultType.TIMEOUT, probability=self.single_timeout_prob)
        )

        injected = 0
        recovered = 0
        degradation_triggered = False
        user_interrupt = False
        n = self.single_timeout_requests

        for i in range(n):
            try:
                _resp, was_fault, degraded = await self._call_agent(f"single_timeout test {i}")
                if was_fault:
                    injected += 1
                    recovered += 1
                    if degraded:
                        degradation_triggered = True
            except Exception:
                injected += 1
                user_interrupt = True

        self._restore_faults(backup)
        duration = time.time() - t0
        passed = (recovered >= injected) and not user_interrupt
        # 平均恢复时间 = 总耗时 / 注入故障数
        recovery_time = duration / max(injected, 1) if injected > 0 else 0.0
        return ScenarioResult(
            name="single_timeout", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={
                "requests": n,
                "fault_rate": self.single_timeout_prob,
            },
        )

    async def _scenario_burst_errors(self) -> ScenarioResult:
        """场景 2: 连续 N 次错误 → 验证降级触发

        注入 100% RATE_LIMIT, 跑 N 次连续请求, 验证降级路径被触发
        """
        t0 = time.time()
        backup = self._backup_faults()
        self.fault_client._faults.clear()
        self.fault_client.add_fault(
            FaultConfig(FaultType.RATE_LIMIT, probability=1.0)
        )

        injected = 0
        recovered = 0
        degradation_triggered = False
        user_interrupt = False
        n = self.burst_errors_count

        for i in range(n):
            try:
                _resp, was_fault, degraded = await self._call_agent(f"burst test {i}")
                if was_fault:
                    injected += 1
                    recovered += 1
                    if degraded:
                        degradation_triggered = True
            except Exception:
                injected += 1
                user_interrupt = True

        self._restore_faults(backup)
        duration = time.time() - t0
        passed = degradation_triggered and (recovered >= injected)
        recovery_time = duration / max(injected, 1) if injected > 0 else 0.0
        return ScenarioResult(
            name="burst_errors", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={"requests": n, "fault_type": "rate_limit"},
        )

    async def _scenario_slow_response(self) -> ScenarioResult:
        """场景 3: 10s 慢响应 → 验证超时处理

        用慢响应 mock client 模拟 10s 延迟 (测试中用 100ms),
        验证 agent 能在合理超时内处理
        """
        t0 = time.time()
        original_real = self.fault_client._real
        backup = self._backup_faults()
        slow_ms = self.slow_response_ms

        class _SlowClient:
            """模拟慢响应客户端"""
            async def complete(self, messages: Any, **kwargs: Any) -> dict:
                """模拟慢响应: 延迟后返回固定结果."""
                await asyncio.sleep(slow_ms / 1000.0)
                return {"choices": [{"message": {"content": "slow ok"}}]}

        self.fault_client._real = _SlowClient()
        self.fault_client._faults.clear()

        injected = 0
        recovered = 0
        degradation_triggered = False
        user_interrupt = False

        # 超时阈值 = 慢响应延迟 + 缓冲 (测试中给 2s)
        timeout_threshold = max(2.0, slow_ms / 1000.0 + 1.0)
        t_start = time.time()
        try:
            try:
                _resp, _was_fault, _degraded = await asyncio.wait_for(
                    self._call_agent("slow response test"),
                    timeout=timeout_threshold,
                )
                elapsed = time.time() - t_start
                # 慢响应成功, 但视为一次"延迟故障"被恢复
                if elapsed >= slow_ms / 1000.0 * 0.5:
                    injected = 1
                    recovered = 1
                else:
                    recovered = 1
            except TimeoutError:
                injected = 1
                # 超时后通过降级恢复
                try:
                    _resp, _was_fault, _degraded = self._try_degraded_reply("slow_response_timeout")
                    recovered = 1
                    degradation_triggered = True
                except Exception:
                    user_interrupt = True
        finally:
            self.fault_client._real = original_real
            self._restore_faults(backup)

        duration = time.time() - t0
        passed = recovered >= injected
        recovery_time = duration / max(injected, 1) if injected > 0 else 0.0
        return ScenarioResult(
            name="slow_response", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={"simulated_slow_ms": slow_ms},
        )

    async def _scenario_partial_failure(self) -> ScenarioResult:
        """场景 4: 50% 故障率 → 验证混合降级"""
        t0 = time.time()
        backup = self._backup_faults()
        self.fault_client._faults.clear()
        self.fault_client.add_fault(
            FaultConfig(FaultType.EMPTY_RESPONSE, probability=self.partial_failure_prob)
        )

        injected = 0
        recovered = 0
        degradation_triggered = False
        user_interrupt = False
        n = self.partial_failure_requests

        for i in range(n):
            try:
                _resp, was_fault, degraded = await self._call_agent(f"partial {i}")
                if was_fault:
                    injected += 1
                    recovered += 1
                    if degraded:
                        degradation_triggered = True
            except Exception:
                injected += 1
                user_interrupt = True

        self._restore_faults(backup)
        duration = time.time() - t0
        # 通过条件: 80% 以上故障恢复
        recovery_rate = recovered / max(injected, 1) if injected > 0 else 1.0
        passed = recovery_rate >= 0.8 and not user_interrupt
        recovery_time = duration / max(injected, 1) if injected > 0 else 0.0
        return ScenarioResult(
            name="partial_failure", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={
                "requests": n,
                "fault_rate": self.partial_failure_prob,
                "recovery_rate": round(recovery_rate, 3),
            },
        )

    async def _scenario_cascading_failure(self) -> ScenarioResult:
        """场景 5: 多组件故障 → 验证级联恢复"""
        t0 = time.time()
        backup = self._backup_faults()
        self.fault_client._faults.clear()
        # 注入多种故障 (各 30% 概率)
        self.fault_client.add_fault(
            FaultConfig(FaultType.TIMEOUT, probability=self.cascading_failure_prob)
        )
        self.fault_client.add_fault(
            FaultConfig(FaultType.RATE_LIMIT, probability=self.cascading_failure_prob)
        )
        self.fault_client.add_fault(
            FaultConfig(FaultType.EMPTY_RESPONSE, probability=self.cascading_failure_prob)
        )

        injected = 0
        recovered = 0
        degradation_triggered = False
        user_interrupt = False
        n = self.cascading_failure_requests

        for i in range(n):
            try:
                _resp, was_fault, degraded = await self._call_agent(f"cascade {i}")
                if was_fault:
                    injected += 1
                    recovered += 1
                    if degraded:
                        degradation_triggered = True
            except Exception:
                injected += 1
                user_interrupt = True

        self._restore_faults(backup)
        duration = time.time() - t0
        # 多故障场景容忍稍低: 70% 恢复即通过
        recovery_rate = recovered / max(injected, 1) if injected > 0 else 1.0
        passed = recovery_rate >= 0.7 and not user_interrupt
        recovery_time = duration / max(injected, 1) if injected > 0 else 0.0
        return ScenarioResult(
            name="cascading_failure", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={
                "requests": n,
                "fault_types": ["timeout", "rate_limit", "empty"],
                "recovery_rate": round(recovery_rate, 3),
            },
        )

    async def _scenario_recovery_test(self) -> ScenarioResult:
        """场景 6: 故障后恢复 → 验证恢复时间

        阶段 1: 注入 100% 故障, 跑 N 次请求
        阶段 2: 清除故障, 测首次成功响应延迟 (即恢复时间)
        """
        t0 = time.time()
        backup = self._backup_faults()

        # 阶段 1: 注入故障
        self.fault_client._faults.clear()
        self.fault_client.add_fault(
            FaultConfig(FaultType.RATE_LIMIT, probability=1.0)
        )

        injected = 0
        recovered = 0
        degradation_triggered = False
        user_interrupt = False

        for i in range(self.recovery_test_fault_count):
            try:
                _resp, was_fault, _degraded = await self._call_agent(f"fault phase {i}")
                if was_fault:
                    injected += 1
                    recovered += 1
                    if _degraded:
                        degradation_triggered = True
            except Exception:
                injected += 1
                user_interrupt = True

        # 阶段 2: 清除故障, 测恢复延迟
        self.fault_client._faults.clear()
        recovery_start = time.time()
        recovery_ok = False
        try:
            _resp, was_fault, _degraded = await self._call_agent("recovery phase")
            recovery_ok = not was_fault
        except Exception:
            recovery_ok = False
        recovery_latency = time.time() - recovery_start

        self._restore_faults(backup)
        duration = time.time() - t0
        passed = recovery_ok and (recovered >= injected)
        # recovery_time = 故障恢复延迟 (从清除故障到首次成功响应)
        recovery_time = recovery_latency if recovery_ok else duration
        return ScenarioResult(
            name="recovery_test", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={
                "fault_phase_requests": self.recovery_test_fault_count,
                "recovery_latency": round(recovery_latency, 4),
                "recovery_ok": recovery_ok,
            },
        )

    async def _scenario_sustained_load(self) -> ScenarioResult:
        """场景 7: 持续负载 100 请求 → 验证稳定性"""
        t0 = time.time()
        backup = self._backup_faults()
        self.fault_client._faults.clear()
        self.fault_client.add_fault(
            FaultConfig(FaultType.EMPTY_RESPONSE, probability=self.sustained_load_prob)
        )

        injected = 0
        recovered = 0
        failed_count = 0
        degradation_triggered = False
        user_interrupt = False
        n = self.sustained_load_requests

        for i in range(n):
            try:
                _resp, was_fault, degraded = await self._call_agent(f"load {i}")
                if was_fault:
                    injected += 1
                    recovered += 1
                    if degraded:
                        degradation_triggered = True
            except Exception:
                injected += 1
                failed_count += 1
                user_interrupt = True

        self._restore_faults(backup)
        duration = time.time() - t0
        # success_rate = 成功请求数 / 总请求数 (含降级恢复)
        #   - 正常请求 + 降级恢复的请求 = n - failed_count
        #   - failed_count = 抛出异常的请求数 (未恢复)
        success_rate = (n - failed_count) / n
        # 故障恢复率 (recovered / injected)
        recovery_rate = recovered / max(injected, 1) if injected > 0 else 1.0
        # 持续负载通过条件: 90% 请求成功 + 90% 故障恢复
        passed = success_rate >= 0.9 and recovery_rate >= 0.9 and not user_interrupt
        recovery_time = duration / max(injected, 1) if injected > 0 else 0.0
        return ScenarioResult(
            name="sustained_load", passed=passed,
            duration=duration, faults_injected=injected,
            faults_recovered=recovered,
            degradation_triggered=degradation_triggered,
            recovery_time=recovery_time,
            user_perceived_interrupt=user_interrupt,
            details={
                "requests": n,
                "fault_rate": self.sustained_load_prob,
                "success_rate": round(success_rate, 3),
                "recovery_rate": round(recovery_rate, 3),
            },
        )
