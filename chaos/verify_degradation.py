"""降级触发验证 — 通过故障注入验证 DegradationDetector / DegradationStrategy 正确触发

本模块不依赖真实 LLM client, 使用 _StubLLMClient 提供正常响应,
通过 FaultInjectingLLMClient 注入故障, 验证三轴退化检测与降级策略:

- timeout → reliability 轴退化 (timeout_rate 升高)
- error   → quality 轴退化 (error_rate 升高)
- slow    → performance 轴退化 (p99_latency 升高)
- 持续注入 → DegradationStrategy 触发降级 (L0 → L1/L2)

参考:
- core/degradation_detector.py: 三轴退化检测 (Axis.QUALITY/PERFORMANCE/RELIABILITY)
- core/degradation_strategy.py: evaluate_from_detector() 自动触发降级
- chaos/fault_injecting_llm_client.py: 故障注入

用法:
    from chaos.verify_degradation import verify_degradation_triggers
    report = verify_degradation_triggers()
    if report["all_passed"]:
        print("降级触发验证通过")
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from chaos.fault_injecting_llm_client import (
    FaultConfig,
    FaultInjectingLLMClient,
    LLMFaultError,
)
from core.degradation_detector import (
    Axis,
    DegradationDetector,
    Severity,
)
from core.degradation_strategy import (
    DegradationLevel,
    DegradationStrategy,
)

# ============================================================
# 辅助: 桩 LLM 客户端 (提供正常响应, 不依赖真实 API)
# ============================================================

class _StubLLMClient:
    """桩 LLM 客户端 — chat 返回固定字符串, chat_stream 产出一个 chunk"""

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        """返回固定字符串 'stub-ok', 用于验证流程不依赖真实 API."""
        return "stub-ok"

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """异步产出单个 chunk 'stub-ok'."""
        # 必须是 async generator
        yield "stub-ok"


# ============================================================
# 辅助: 为三轴预设稳定基线
# ============================================================

def _seed_stable_baselines(detector: DegradationDetector) -> None:
    """为 DegradationDetector 预设稳定基线 (便于退化精确触发)

    基线值参考 tests/test_degradation_detector.py::_seed_all
    """
    # Quality
    detector.seed_baseline(Axis.QUALITY, "error_rate", mean=0.01, std=0.005)
    # Performance
    detector.seed_baseline(Axis.PERFORMANCE, "p99_latency", mean=200.0, std=50.0)
    # Reliability
    detector.seed_baseline(Axis.RELIABILITY, "timeout_rate", mean=0.01, std=0.005)
    detector.seed_baseline(Axis.RELIABILITY, "success_rate", mean=0.99, std=0.005)


# ============================================================
# 验证主流程
# ============================================================

async def _verify_timeout_reliability() -> dict:
    """场景 a: 注入 timeout → 验证 reliability 轴退化检测

    流程:
    1. 创建 100% timeout 故障的 client
    2. 调用 chat, 捕获 TimeoutError
    3. 每次捕获后, 记录 timeout_rate / success_rate 到 detector (高/低值)
    4. 每次记录后立即 check_degradation() (EWMA 基线会随记录漂移,
       首次记录后 z-score 最高, 后续逐渐降低)
    5. 验证至少一次报告的 RELIABILITY 轴受影响

    设计说明: EWMA 基线 (alpha=0.1) 在每次记录后向新值漂移,
    若只在 3 次记录后检查, z-score 可能已降至 2σ 以下.
    因此在每次记录后立即检查, 首次检查 z-score ≈ 3.0 必然触发.
    """
    detector = DegradationDetector()
    _seed_stable_baselines(detector)

    cfg = FaultConfig(fault_rate=1.0, fault_types=["timeout"], seed=42)
    client = FaultInjectingLLMClient(_StubLLMClient(), cfg)

    timeout_count = 0
    reliability_triggered = False
    last_report = None
    for _ in range(3):
        try:
            await client.chat(messages=[{"role": "user", "content": "hi"}])
        except TimeoutError:
            timeout_count += 1
            # 每次超时, 记录高 timeout_rate 与低 success_rate (模拟 SLOTracker 拉取)
            detector.record_reliability("timeout_rate", 0.5)
            detector.record_reliability("success_rate", 0.5)
            # 每次记录后立即检查 (EWMA 会漂移, 首次检查最敏感)
            report = detector.check_degradation()
            last_report = report
            if (
                report.severity != Severity.NONE
                and Axis.RELIABILITY in report.affected_axes
            ):
                reliability_triggered = True

    passed = (
        timeout_count == 3
        and reliability_triggered
    )
    return {
        "name": "timeout_reliability",
        "passed": passed,
        "timeout_count": timeout_count,
        "reliability_triggered": reliability_triggered,
        "severity": last_report.severity.value if last_report else "none",
        "affected_axes": (
            [a.value for a in last_report.affected_axes] if last_report else []
        ),
        "stats": client.get_stats(),
    }


async def _verify_error_quality() -> dict:
    """场景 b: 注入 error → 验证 quality 轴退化检测

    流程:
    1. 创建 100% error 故障的 client
    2. 调用 chat, 捕获 LLMFaultError
    3. 每次捕获后, 记录 error_rate 到 detector (0.5, 远超基线 0.01)
    4. 每次记录后立即 check_degradation()
    5. 验证至少一次报告的 QUALITY 轴受影响
    """
    detector = DegradationDetector()
    _seed_stable_baselines(detector)

    cfg = FaultConfig(fault_rate=1.0, fault_types=["error"], seed=7)
    client = FaultInjectingLLMClient(_StubLLMClient(), cfg)

    error_count = 0
    quality_triggered = False
    last_report = None
    for _ in range(3):
        try:
            await client.chat(messages=[{"role": "user", "content": "hi"}])
        except LLMFaultError:
            error_count += 1
            detector.record_quality("error_rate", 0.5)
            report = detector.check_degradation()
            last_report = report
            if (
                report.severity != Severity.NONE
                and Axis.QUALITY in report.affected_axes
            ):
                quality_triggered = True

    passed = (
        error_count == 3
        and quality_triggered
    )
    return {
        "name": "error_quality",
        "passed": passed,
        "error_count": error_count,
        "quality_triggered": quality_triggered,
        "severity": last_report.severity.value if last_report else "none",
        "affected_axes": (
            [a.value for a in last_report.affected_axes] if last_report else []
        ),
        "stats": client.get_stats(),
    }


async def _verify_slow_performance() -> dict:
    """场景 c: 注入 slow → 验证 performance 轴退化检测

    注意: slow 故障会延迟 10s, 此处通过 mock SLOW_FAULT_DELAY_SECONDS=0
    避免真实等待.

    流程:
    1. 创建 100% slow 故障的 client
    2. 调用 chat, 延迟后返回正常响应
    3. 每次调用后, 记录 p99_latency 到 detector (高值)
    4. 每次记录后立即 check_degradation()
    5. 验证至少一次报告的 PERFORMANCE 轴受影响
    """
    detector = DegradationDetector()
    _seed_stable_baselines(detector)

    # 使用 seed=None 保证 fault_rate=1.0 时一定注入
    cfg = FaultConfig(fault_rate=1.0, fault_types=["slow"], seed=None)
    client = FaultInjectingLLMClient(_StubLLMClient(), cfg)

    # 临时将延迟置零, 避免真实等待 10s (测试快速通过)
    import chaos.fault_injecting_llm_client as fim
    original_delay = fim.SLOW_FAULT_DELAY_SECONDS
    fim.SLOW_FAULT_DELAY_SECONDS = 0.0
    try:
        slow_count = 0
        performance_triggered = False
        last_report = None
        for _ in range(3):
            reply = await client.chat(messages=[{"role": "user", "content": "hi"}])
            if reply == "stub-ok":
                slow_count += 1
                # 记录高 p99_latency (模拟 SLOTracker 拉取)
                detector.record_performance("p99_latency", 2000.0)
                report = detector.check_degradation()
                last_report = report
                if (
                    report.severity != Severity.NONE
                    and Axis.PERFORMANCE in report.affected_axes
                ):
                    performance_triggered = True
    finally:
        fim.SLOW_FAULT_DELAY_SECONDS = original_delay

    passed = (
        slow_count == 3
        and performance_triggered
    )
    return {
        "name": "slow_performance",
        "passed": passed,
        "slow_count": slow_count,
        "performance_triggered": performance_triggered,
        "severity": last_report.severity.value if last_report else "none",
        "affected_axes": (
            [a.value for a in last_report.affected_axes] if last_report else []
        ),
        "stats": client.get_stats(),
    }


async def _verify_continuous_strategy() -> dict:
    """场景 d: 持续注入 → 验证 DegradationStrategy 触发降级

    流程:
    1. 创建多类型故障 client (fault_rate=1.0, 4 种故障)
    2. wire detector.on_degradation → strategy.evaluate_from_detector
    3. 持续调用 chat, 捕获故障, 记录对应轴指标
    4. 多次 check_degradation 后, strategy 应从 L0 升级到 L1/L2
    """
    detector = DegradationDetector()
    _seed_stable_baselines(detector)

    strategy = DegradationStrategy()
    initial_level = strategy.current_level

    # 接线: detector 退化 → strategy 自动评估
    def _on_degradation(_report: Any) -> None:
        strategy.evaluate_from_detector(detector)

    detector.on_degradation(_on_degradation)

    cfg = FaultConfig(fault_rate=1.0, fault_types=["timeout", "error", "slow", "empty"], seed=11)
    client = FaultInjectingLLMClient(_StubLLMClient(), cfg)

    # 临时将 slow 延迟置零
    import chaos.fault_injecting_llm_client as fim
    original_delay = fim.SLOW_FAULT_DELAY_SECONDS
    fim.SLOW_FAULT_DELAY_SECONDS = 0.0
    try:
        for i in range(10):
            try:
                reply = await client.chat(messages=[{"role": "user", "content": f"msg-{i}"}])
                # 正常返回 (slow 或 empty): 记录低延迟与高成功率
                if reply == "":
                    # empty 视为质量问题
                    detector.record_quality("error_rate", 0.4)
                else:
                    detector.record_performance("p99_latency", 100.0)
                    detector.record_reliability("success_rate", 0.99)
            except TimeoutError:
                detector.record_reliability("timeout_rate", 0.6)
                detector.record_reliability("success_rate", 0.4)
            except LLMFaultError:
                detector.record_quality("error_rate", 0.5)
            # 每轮触发一次退化检测 (会回调 _on_degradation)
            detector.check_degradation()
    finally:
        fim.SLOW_FAULT_DELAY_SECONDS = original_delay

    final_level = strategy.current_level
    stats = client.get_stats()

    passed = (
        stats["faults_injected"] >= 1
        and final_level > DegradationLevel.L0_NORMAL
        and final_level > initial_level
    )
    return {
        "name": "continuous_strategy",
        "passed": passed,
        "initial_level": initial_level.name,
        "final_level": final_level.name,
        "reason": strategy.reason,
        "stats": stats,
    }


# ============================================================
# 公共入口
# ============================================================

async def verify_degradation_triggers_async() -> dict:
    """异步入口: 运行所有降级触发验证场景, 返回汇总报告

    返回结构:
        {
            "scenarios": [每场景结果],
            "all_passed": bool,
            "summary": str,
        }
    """
    scenarios = [
        await _verify_timeout_reliability(),
        await _verify_error_quality(),
        await _verify_slow_performance(),
        await _verify_continuous_strategy(),
    ]
    all_passed = all(s.get("passed", False) for s in scenarios)
    summary = (
        "全部通过" if all_passed
        else f"{sum(1 for s in scenarios if s.get('passed', False))}/{len(scenarios)} 通过"
    )
    logger.info(
        f"verify_degradation_triggers: {summary} "
        f"scenarios={[s['name'] for s in scenarios]}"
    )
    return {
        "scenarios": scenarios,
        "all_passed": all_passed,
        "summary": summary,
    }


def verify_degradation_triggers() -> dict:
    """同步入口: 运行所有降级触发验证场景 (内部启动 asyncio loop)

    返回结构同 verify_degradation_triggers_async().
    若当前已在事件循环中 (如 jupyter / async 测试), 请改用
    verify_degradation_triggers_async().
    """
    return asyncio.run(verify_degradation_triggers_async())


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import json
    report = verify_degradation_triggers()
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    sys.exit(0 if report["all_passed"] else 1)
