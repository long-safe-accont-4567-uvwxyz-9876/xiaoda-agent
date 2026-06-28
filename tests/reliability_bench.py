"""ReliabilityBench — 三维可靠面评估

pass@k: 任务完成率
鲁棒性: 在故障注入下的表现
容错: 错误恢复能力
"""
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class ReliabilityResult:
    """可靠性评估结果"""
    pass_at_k: float = 0.0       # 任务完成率
    robustness: float = 0.0       # 故障下表现
    fault_tolerance: float = 0.0  # 错误恢复

    @property
    def overall_score(self) -> float:
        return (self.pass_at_k * 0.4 + self.robustness * 0.3 + self.fault_tolerance * 0.3)

    @property
    def grade(self) -> str:
        s = self.overall_score
        if s >= 0.9: return "A"
        if s >= 0.8: return "B"
        if s >= 0.7: return "C"
        if s >= 0.6: return "D"
        return "F"


class ReliabilityBench:
    """三维可靠性基准测试"""

    def __init__(self):
        self._results: list[dict] = []

    async def run_test_suite(self, test_cases: list[dict], executor) -> ReliabilityResult:
        """运行测试套件"""
        total = len(test_cases)
        passed = 0
        robust_passed = 0
        recovery_passed = 0

        for tc in test_cases:
            try:
                result = await executor(tc)
                if result.get("success"):
                    passed += 1
                if result.get("handled_fault"):
                    robust_passed += 1
                if result.get("recovered"):
                    recovery_passed += 1
            except Exception as e:
                logger.debug(f"测试失败: {tc.get('name', '?')}: {e}")

        result = ReliabilityResult(
            pass_at_k=passed / total if total else 0,
            robustness=robust_passed / total if total else 0,
            fault_tolerance=recovery_passed / total if total else 0,
        )
        logger.info(f"ReliabilityBench: pass@k={result.pass_at_k:.0%} "
                     f"robustness={result.robustness:.0%} "
                     f"fault_tolerance={result.fault_tolerance:.0%} "
                     f"grade={result.grade}")
        return result
