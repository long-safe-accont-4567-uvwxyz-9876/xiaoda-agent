"""ReliabilityBench 命令行入口 (Ch2 P1 Chaos Engineering)

用法:
    python -m chaos.run_bench                                # 运行所有场景, 输出彩色报告
    python -m chaos.run_bench --scenario single_timeout      # 运行单个场景
    python -m chaos.run_bench --json                          # JSON 格式输出

退出码:
    0 : overall_score >= 60
    1 : overall_score < 60 或运行异常
"""
from __future__ import annotations
from typing import Any

import argparse
import asyncio
import json
import os
import sys

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from chaos.reliability_bench import BenchReport, ReliabilityBench  # noqa: E402
from chaos._fault_types import (  # noqa: E402
    FaultConfig,
    FaultInjectingLLMClient,
    FaultType,
)


# ============================================================
# ANSI 彩色输出
# ============================================================

def _color(text: str, code: str) -> str:
    """简单 ANSI 颜色包装 (不支持的终端会忽略)"""
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(text: str) -> str:
    return _color(text, "32")


def _red(text: str) -> str:
    return _color(text, "31")


def _yellow(text: str) -> str:
    return _color(text, "33")


def _cyan(text: str) -> str:
    return _color(text, "36")


def _bold(text: str) -> str:
    return _color(text, "1")


def _dim(text: str) -> str:
    return _color(text, "2")


# ============================================================
# 报告打印
# ============================================================

def print_report(report: BenchReport) -> None:
    """打印彩色报告到终端"""
    width = 64
    print(_cyan("=" * width))
    print(_bold(_cyan("ReliabilityBench 三维可靠性评估报告")))
    print(_cyan("=" * width))

    # 场景结果
    print(_bold("\n【场景结果】"))
    for r in report.scenario_results:
        status = _green("PASS") if r.passed else _red("FAIL")
        degr = _green("Y") if r.degradation_triggered else _red("N")
        interrupt = _red("Y") if r.user_perceived_interrupt else _green("N")
        print(f"  [{status}] {r.name}")
        print(_dim(
            f"           duration={r.duration:.3f}s "
            f"faults={r.faults_injected}/{r.faults_recovered} "
            f"degradation={degr} "
            f"interrupt={interrupt} "
            f"recovery_time={r.recovery_time:.3f}s"
        ))

    # 三维评分
    print(_bold("\n【三维评分】"))
    scores = report.three_axis_scores
    ft = scores.get("fault_tolerance", 0.0)
    rs = scores.get("recovery_speed", 0.0)
    dg = scores.get("degradation_gracefulness", 0.0)

    def _score_color(s: float) -> str:
        if s >= 80:
            return _green(f"{s:.1f}/100")
        if s >= 60:
            return _yellow(f"{s:.1f}/100")
        return _red(f"{s:.1f}/100")

    print(f"  容错性 (Fault Tolerance):        {_score_color(ft)}")
    print(f"  恢复速度 (Recovery Speed):      {_score_color(rs)}")
    print(f"  降级优雅度 (Gracefulness):      {_score_color(dg)}")
    print(_bold(f"\n  综合评分: {_score_color(report.overall_score)}"))

    # 建议
    if report.recommendations:
        print(_bold("\n【改进建议】"))
        for i, rec in enumerate(report.recommendations, 1):
            print(f"  {_yellow(str(i))}. {rec}")

    print(_cyan("=" * width))


# ============================================================
# 默认 mock agent (CLI 演示用)
# ============================================================

class _DemoAgent:
    """演示用 mock agent — 提供降级回复 (用于 CLI 默认运行)"""

    def __init__(self) -> None:
        self._degrade_count = 0

    def degraded_reply(self, error: str) -> str:
        """降级兜底回复"""
        self._degrade_count += 1
        return f"[降级回复] 当前服务繁忙, 稍后重试 (cause: {error[:50]})"


class _DemoLLMClient:
    """演示用 mock LLM 客户端"""

    async def complete(self, messages: Any, **kwargs: Any) -> dict:
        """返回固定演示响应, 供 CLI 默认运行使用."""
        return {"choices": [{"message": {"content": "demo response"}}]}


# ============================================================
# 主入口
# ============================================================

async def _run(args: Any) -> int:
    """运行基准测试"""
    agent = _DemoAgent()
    real_client = _DemoLLMClient()
    fault_client = FaultInjectingLLMClient(real_client)
    bench = ReliabilityBench(agent=agent, fault_client=fault_client)

    scenarios = [args.scenario] if args.scenario else None
    report = await bench.run_suite(scenarios=scenarios)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_report(report)

    return 0 if report.overall_score >= 60 else 1


def main() -> int:
    """CLI 入口: 解析参数并运行可靠性基准测试."""
    parser = argparse.ArgumentParser(
        description="ReliabilityBench 三维可靠性评估",
        prog="python -m chaos.run_bench",
    )
    parser.add_argument(
        "--scenario", type=str, default=None,
        help="运行单个场景 (single_timeout/burst_errors/slow_response/"
             "partial_failure/cascading_failure/recovery_test/sustained_load)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="JSON 格式输出 (适合 CI 解析)",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n中断")
        return 130
    except Exception as e:
        print(f"运行异常: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
