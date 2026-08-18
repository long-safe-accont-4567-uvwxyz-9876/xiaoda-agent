"""TNR 测试场景 — 三类故障的 TNR 自愈流程

场景:
- tnr_timeout:    超时故障的 TNR 流程 (高延迟/低成功率)
- tnr_error:      错误故障的 TNR 流程 (高错误率)
- tnr_cascading:   级联故障的 TNR 流程 (多组件故障, 最严重)

每个场景通过工厂函数构建一个完整可运行的 TNRProtocol 实例:
- 注入对应故障类型的指标 (健康度从 EXCELLENT 降到 CRITICAL/POOR)
- 自愈后指标恢复到故障前水平 (健康度不降)

用法:
    from chaos.tnr_scenarios import make_scenario, run_scenario

    protocol = make_scenario("tnr_timeout")
    report = await run_scenario("tnr_timeout")
    print(report.health_restored)
"""
from __future__ import annotations

import asyncio

from core.behavioral_health import BehavioralHealthScorer
from core.degradation_strategy import DegradationStrategy, reset_degradation_strategy
from core.recovery_orchestrator import RecoveryOrchestrator

from chaos.tnr_protocol import TNRProtocol, TNRReport


# ============================================================
# 场景定义
# ============================================================

# 各场景对应的故障类型 (传给 run_protocol)
SCENARIO_FAULT_TYPES: dict[str, str] = {
    "tnr_timeout": "timeout",
    "tnr_error": "error",
    "tnr_cascading": "cascading",
}

# 场景描述 (用于日志/报告)
SCENARIO_DESCRIPTIONS: dict[str, str] = {
    "tnr_timeout": "超时故障的 TNR 流程: 高延迟/低成功率, 验证自愈后恢复",
    "tnr_error": "错误故障的 TNR 流程: 高错误率, 验证自愈后恢复",
    "tnr_cascading": "级联故障的 TNR 流程: 多组件故障 (最严重), 验证自愈后恢复",
}

DEFAULT_SCENARIOS: list[str] = ["tnr_timeout", "tnr_error", "tnr_cascading"]


# ============================================================
# 工厂函数
# ============================================================

def make_scenario(
    name: str,
    health_scorer: BehavioralHealthScorer | None = None,
    recovery_orchestrator: RecoveryOrchestrator | None = None,
    degradation_strategy: DegradationStrategy | None = None,
) -> TNRProtocol:
    """构建指定场景的 TNRProtocol 实例

    Args:
        name: 场景名 (tnr_timeout / tnr_error / tnr_cascading)
        health_scorer:        可选, 默认新建
        recovery_orchestrator: 可选, 默认新建
        degradation_strategy:  可选, 默认新建 (重置全局单例)

    Returns:
        配置好默认故障指标的 TNRProtocol 实例
    """
    if name not in SCENARIO_FAULT_TYPES:
        raise ValueError(
            f"未知场景: {name}, 合法场景: {list(SCENARIO_FAULT_TYPES)}"
        )

    scorer = health_scorer or BehavioralHealthScorer()
    orchestrator = recovery_orchestrator or RecoveryOrchestrator()
    strategy = degradation_strategy or reset_degradation_strategy()

    return TNRProtocol(
        health_scorer=scorer,
        recovery_orchestrator=orchestrator,
        degradation_strategy=strategy,
    )


def make_timeout_scenario(**kwargs) -> TNRProtocol:
    """超时故障场景: 高延迟/低成功率"""
    return make_scenario("tnr_timeout", **kwargs)


def make_error_scenario(**kwargs) -> TNRProtocol:
    """错误故障场景: 高错误率"""
    return make_scenario("tnr_error", **kwargs)


def make_cascading_scenario(**kwargs) -> TNRProtocol:
    """级联故障场景: 多组件故障 (最严重)"""
    return make_scenario("tnr_cascading", **kwargs)


# ============================================================
# 运行入口
# ============================================================

async def run_scenario(name: str) -> TNRReport:
    """运行单个 TNR 场景

    Args:
        name: 场景名 (tnr_timeout / tnr_error / tnr_cascading)

    Returns:
        TNRReport
    """
    fault_type = SCENARIO_FAULT_TYPES[name]
    protocol = make_scenario(name)
    return await protocol.run_protocol(fault_type)


async def run_all_scenarios(
    scenarios: list[str] | None = None,
) -> dict[str, TNRReport]:
    """运行所有 TNR 场景

    Args:
        scenarios: 场景列表, None 则运行全部

    Returns:
        {场景名: TNRReport}
    """
    names = scenarios if scenarios is not None else list(DEFAULT_SCENARIOS)
    results: dict[str, TNRReport] = {}
    for name in names:
        results[name] = await run_scenario(name)
    return results


def run_scenario_sync(name: str) -> TNRReport:
    """运行单个 TNR 场景 (同步包装)"""
    return asyncio.run(run_scenario(name))


def run_all_scenarios_sync(
    scenarios: list[str] | None = None,
) -> dict[str, TNRReport]:
    """运行所有 TNR 场景 (同步包装)"""
    return asyncio.run(run_all_scenarios(scenarios))
