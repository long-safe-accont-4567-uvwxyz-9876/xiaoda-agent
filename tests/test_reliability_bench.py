"""ReliabilityBench 三维可靠性评估单元测试 (Ch2 P1 Chaos Engineering)

覆盖场景:
- test_single_timeout_scenario      : 单次超时场景
- test_burst_errors_scenario         : 连续错误场景
- test_recovery_scenario             : 恢复测试场景
- test_three_axis_scores             : 三维评分计算正确
- test_recommendations_generated    : 建议生成
- test_overall_score                 : 综合评分计算
- test_run_suite_all_scenarios       : 完整套件运行
- test_run_suite_unknown_scenario    : 未知场景跳过

测试策略:
- 使用 mock agent (含 degraded_reply 方法), 不真实调用 LLM
- 通过构造函数参数固定场景行为 (probability=1.0 等) 以保证确定性
- 通过构造已知 ScenarioResult 直接验证评分计算
"""
import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chaos.reliability_bench import (
    BenchReport,
    DEFAULT_SCENARIOS,
    ReliabilityBench,
    ScenarioResult,
)
from tests.fault_injection import (
    FaultInjectingLLMClient,
)


# ============================================================
# Mock fixtures
# ============================================================

class _MockLLMClient:
    """Mock LLM 客户端 — 直接返回成功响应"""

    def __init__(self):
        self.call_count = 0

    async def complete(self, messages, **kwargs):
        self.call_count += 1
        return {"choices": [{"message": {"content": "mock response"}}]}


class _MockAgent:
    """Mock agent — 提供 degraded_reply 降级兜底"""

    def __init__(self):
        self.degrade_count = 0

    def degraded_reply(self, error: str) -> str:
        """降级兜底回复 (非空表示降级成功)"""
        self.degrade_count += 1
        return f"[降级] 服务繁忙, 稍后重试 (cause={error[:30]})"


class _MockAgentNoDegrade:
    """Mock agent — 不提供降级回复"""



@pytest.fixture
def mock_client():
    return _MockLLMClient()


@pytest.fixture
def mock_agent():
    return _MockAgent()


@pytest.fixture
def fault_client(mock_client):
    """干净的 FaultInjectingLLMClient (无故障)"""
    return FaultInjectingLLMClient(mock_client)


@pytest.fixture
def bench(mock_agent, fault_client):
    """配置为快速+确定性的 bench (小请求数, 高故障率)"""
    return ReliabilityBench(
        agent=mock_agent,
        fault_client=fault_client,
        single_timeout_prob=1.0,        # 必然超时
        single_timeout_requests=5,
        burst_errors_count=3,
        slow_response_ms=20,             # 20ms 慢响应
        partial_failure_prob=1.0,        # 必然空响应
        partial_failure_requests=5,
        cascading_failure_prob=1.0,      # 必然级联故障
        cascading_failure_requests=5,
        recovery_test_fault_count=2,
        sustained_load_prob=1.0,
        sustained_load_requests=5,
    )


# ============================================================
# 1. single_timeout 场景
# ============================================================

class TestSingleTimeoutScenario:
    """场景 1: 单次超时 → 验证重试"""

    @pytest.mark.asyncio
    async def test_single_timeout_scenario(self, bench, mock_agent):
        """单次超时场景应通过 (mock agent 提供降级兜底)"""
        result = await bench._scenario_single_timeout()

        assert result.name == "single_timeout"
        # probability=1.0 → 所有请求都超时, 全部触发降级
        assert result.faults_injected == 5
        assert result.faults_recovered == 5
        assert result.degradation_triggered is True
        assert result.user_perceived_interrupt is False
        assert result.passed is True
        assert result.duration > 0
        # degraded_reply 被调用 5 次
        assert mock_agent.degrade_count == 5

    @pytest.mark.asyncio
    async def test_single_timeout_no_degrade_fails(self, mock_client):
        """agent 无 degraded_reply 时, 超时应导致用户中断"""
        agent = _MockAgentNoDegrade()
        fault_client = FaultInjectingLLMClient(mock_client)
        bench = ReliabilityBench(
            agent=agent,
            fault_client=fault_client,
            single_timeout_prob=1.0,
            single_timeout_requests=3,
        )
        result = await bench._scenario_single_timeout()
        assert result.faults_injected == 3
        assert result.faults_recovered == 0
        assert result.user_perceived_interrupt is True
        assert result.degradation_triggered is False
        assert result.passed is False


# ============================================================
# 2. burst_errors 场景
# ============================================================

class TestBurstErrorsScenario:
    """场景 2: 连续错误 → 验证降级触发"""

    @pytest.mark.asyncio
    async def test_burst_errors_scenario(self, bench, mock_agent):
        """连续错误场景应通过 (降级被触发)"""
        result = await bench._scenario_burst_errors()

        assert result.name == "burst_errors"
        # probability=1.0 → 全部 RATE_LIMIT, 全部降级恢复
        assert result.faults_injected == 3
        assert result.faults_recovered == 3
        assert result.degradation_triggered is True
        assert result.user_perceived_interrupt is False
        assert result.passed is True
        assert mock_agent.degrade_count == 3

    @pytest.mark.asyncio
    async def test_burst_errors_no_degrade_fails(self, mock_client):
        """agent 无 degraded_reply 时, 连续错误应失败"""
        agent = _MockAgentNoDegrade()
        fault_client = FaultInjectingLLMClient(mock_client)
        bench = ReliabilityBench(
            agent=agent,
            fault_client=fault_client,
            burst_errors_count=3,
        )
        result = await bench._scenario_burst_errors()
        assert result.faults_injected == 3
        assert result.faults_recovered == 0
        assert result.user_perceived_interrupt is True
        assert result.passed is False


# ============================================================
# 3. recovery_test 场景
# ============================================================

class TestRecoveryScenario:
    """场景 6: 故障后恢复 → 验证恢复时间"""

    @pytest.mark.asyncio
    async def test_recovery_scenario(self, bench, mock_agent):
        """恢复测试场景应通过 (故障清除后能立即恢复)"""
        result = await bench._scenario_recovery_test()

        assert result.name == "recovery_test"
        # 阶段 1: 注入 2 次 RATE_LIMIT, 全部降级
        assert result.faults_injected == 2
        assert result.faults_recovered == 2
        assert result.degradation_triggered is True
        # 阶段 2: 清除故障后能立即恢复
        assert result.passed is True
        assert result.user_perceived_interrupt is False
        assert result.recovery_time >= 0
        # 恢复时间应该非常短 (mock client 立即响应)
        assert result.recovery_time < 1.0

    @pytest.mark.asyncio
    async def test_recovery_details(self, bench):
        """恢复测试 details 包含必要字段"""
        result = await bench._scenario_recovery_test()
        assert "fault_phase_requests" in result.details
        assert "recovery_latency" in result.details
        assert "recovery_ok" in result.details
        assert result.details["recovery_ok"] is True


# ============================================================
# 4. 三维评分计算
# ============================================================

class TestThreeAxisScores:
    """三维评分计算正确性"""

    def test_three_axis_scores_perfect(self, mock_agent, fault_client):
        """全部恢复 + 全部降级 → 三维评分均接近 100"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="s1", passed=True,
                faults_injected=10, faults_recovered=10,
                degradation_triggered=True,
                recovery_time=0.01,
                user_perceived_interrupt=False,
            ),
            ScenarioResult(
                name="s2", passed=True,
                faults_injected=5, faults_recovered=5,
                degradation_triggered=True,
                recovery_time=0.02,
                user_perceived_interrupt=False,
            ),
        ])
        bench.compute_scores(report)
        scores = report.three_axis_scores
        # 容错: (10+5)/(10+5) = 1.0 → 100
        assert scores["fault_tolerance"] == 100.0
        # 降级优雅度: 2/2 * (1-0) = 1.0 → 100
        assert scores["degradation_gracefulness"] == 100.0
        # 恢复速度: 1/(1+0.015) ≈ 0.985 → ~98.5
        assert scores["recovery_speed"] >= 98.0
        assert scores["recovery_speed"] <= 99.0

    def test_three_axis_scores_no_recovery(self, mock_agent, fault_client):
        """全部未恢复 → 容错=0, 优雅度=0"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="s1", passed=False,
                faults_injected=10, faults_recovered=0,
                degradation_triggered=False,
                recovery_time=1.0,
                user_perceived_interrupt=True,
            ),
        ])
        bench.compute_scores(report)
        scores = report.three_axis_scores
        assert scores["fault_tolerance"] == 0.0
        assert scores["degradation_gracefulness"] == 0.0

    def test_three_axis_scores_partial(self, mock_agent, fault_client):
        """部分恢复 → 容错=50%"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="s1", passed=False,
                faults_injected=10, faults_recovered=5,
                degradation_triggered=True,
                recovery_time=0.5,
                user_perceived_interrupt=True,
            ),
        ])
        bench.compute_scores(report)
        scores = report.three_axis_scores
        # 容错: 5/10 = 50%
        assert scores["fault_tolerance"] == 50.0
        # 降级触发率 = 1/1 = 1.0, 中断率 = 1/1 = 1.0
        # 优雅度 = 1.0 * (1 - 1.0) = 0.0
        assert scores["degradation_gracefulness"] == 0.0

    def test_three_axis_scores_keys(self, mock_agent, fault_client):
        """三维评分包含三个键"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(name="s1", passed=True, faults_injected=1, faults_recovered=1),
        ])
        bench.compute_scores(report)
        assert set(report.three_axis_scores.keys()) == {
            "fault_tolerance", "recovery_speed", "degradation_gracefulness"
        }


# ============================================================
# 5. 建议生成
# ============================================================

class TestRecommendations:
    """建议生成"""

    def test_recommendations_generated_low_scores(self, mock_agent, fault_client):
        """低评分时生成多条建议"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="bad_scenario", passed=False,
                faults_injected=10, faults_recovered=2,
                degradation_triggered=False,
                recovery_time=2.0,
                user_perceived_interrupt=True,
            ),
        ])
        bench.compute_scores(report)
        bench.generate_recommendations(report)
        assert len(report.recommendations) >= 3
        # 应包含容错性建议
        assert any("容错性" in r for r in report.recommendations)
        # 应包含恢复速度建议
        assert any("恢复速度" in r for r in report.recommendations)
        # 应包含降级优雅度建议
        assert any("降级优雅度" in r for r in report.recommendations)
        # 应包含失败场景提示
        assert any("bad_scenario" in r for r in report.recommendations)

    def test_recommendations_generated_high_scores(self, mock_agent, fault_client):
        """高评分时生成"保持现状"建议"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="good", passed=True,
                faults_injected=5, faults_recovered=5,
                degradation_triggered=True,
                recovery_time=0.01,
                user_perceived_interrupt=False,
            ),
        ])
        bench.compute_scores(report)
        bench.generate_recommendations(report)
        assert len(report.recommendations) == 1
        assert "良好" in report.recommendations[0] or "保持" in report.recommendations[0]

    def test_recommendations_generated_empty_scenarios(self, mock_agent, fault_client):
        """空场景列表时仍生成默认建议"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[])
        bench.compute_scores(report)
        bench.generate_recommendations(report)
        # 无故障 → 默认良好建议
        assert len(report.recommendations) >= 1


# ============================================================
# 6. 综合评分计算
# ============================================================

class TestOverallScore:
    """综合评分计算"""

    def test_overall_score_perfect(self, mock_agent, fault_client):
        """完美场景: overall_score 接近 100"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="perfect", passed=True,
                faults_injected=10, faults_recovered=10,
                degradation_triggered=True,
                recovery_time=0.001,
                user_perceived_interrupt=False,
            ),
        ])
        bench.compute_scores(report)
        assert report.overall_score >= 99.0
        assert report.overall_score <= 100.0

    def test_overall_score_weighted(self, mock_agent, fault_client):
        """综合评分 = FT*0.4 + RS*0.3 + DG*0.3 (验证加权公式)"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        # 构造: FT=50%, RS=1/(1+1)=50%, DG=0%
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="mid", passed=False,
                faults_injected=10, faults_recovered=5,
                degradation_triggered=True,
                recovery_time=1.0,
                user_perceived_interrupt=True,
            ),
        ])
        bench.compute_scores(report)
        # FT=50, RS=1/(1+1)*100=50, DG=0
        # overall = 50*0.4 + 50*0.3 + 0*0.3 = 20+15+0 = 35
        assert report.overall_score == 35.0

    def test_overall_score_no_faults(self, mock_agent, fault_client):
        """无故障场景: overall_score = 100"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="clean", passed=True,
                faults_injected=0, faults_recovered=0,
                degradation_triggered=False,
                recovery_time=0.0,
                user_perceived_interrupt=False,
            ),
        ])
        bench.compute_scores(report)
        # 无故障: FT=1.0, RS=1.0, DG=1.0 → overall=100
        assert report.overall_score == 100.0

    def test_overall_score_range(self, mock_agent, fault_client):
        """综合评分在 0-100 范围内"""
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        for ft_ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
            report = BenchReport(scenario_results=[
                ScenarioResult(
                    name="t", passed=ft_ratio >= 0.5,
                    faults_injected=100,
                    faults_recovered=int(100 * ft_ratio),
                    degradation_triggered=ft_ratio > 0,
                    recovery_time=0.5,
                    user_perceived_interrupt=ft_ratio < 0.5,
                ),
            ])
            bench.compute_scores(report)
            assert 0.0 <= report.overall_score <= 100.0


# ============================================================
# 7. 完整套件运行
# ============================================================

class TestRunSuite:
    """完整测试套件运行"""

    @pytest.mark.asyncio
    async def test_run_suite_all_scenarios(self, bench):
        """运行所有 7 个内置场景"""
        report = await bench.run_suite()
        assert len(report.scenario_results) == 7
        # 验证所有场景名
        names = [r.name for r in report.scenario_results]
        for expected in DEFAULT_SCENARIOS:
            assert expected in names, f"缺少场景: {expected}"
        # 验证评分已计算
        assert report.three_axis_scores != {}
        assert "fault_tolerance" in report.three_axis_scores
        assert "recovery_speed" in report.three_axis_scores
        assert "degradation_gracefulness" in report.three_axis_scores
        # 验证建议已生成
        assert len(report.recommendations) > 0
        # 验证综合评分范围
        assert 0 <= report.overall_score <= 100

    @pytest.mark.asyncio
    async def test_run_suite_subset(self, bench):
        """运行指定场景子集"""
        report = await bench.run_suite(scenarios=["single_timeout", "recovery_test"])
        assert len(report.scenario_results) == 2
        names = [r.name for r in report.scenario_results]
        assert names == ["single_timeout", "recovery_test"]

    @pytest.mark.asyncio
    async def test_run_suite_unknown_scenario(self, bench):
        """未知场景被跳过 (不报错)"""
        report = await bench.run_suite(scenarios=["nonexistent_scenario"])
        assert len(report.scenario_results) == 0
        # 仍应返回有效报告
        assert report.overall_score >= 0

    @pytest.mark.asyncio
    async def test_run_suite_to_dict(self, bench):
        """to_dict 序列化正常"""
        report = await bench.run_suite(scenarios=["single_timeout"])
        d = report.to_dict()
        assert "scenario_results" in d
        assert "overall_score" in d
        assert "three_axis_scores" in d
        assert "recommendations" in d
        assert len(d["scenario_results"]) == 1
        assert d["scenario_results"][0]["name"] == "single_timeout"

    @pytest.mark.asyncio
    async def test_run_suite_with_real_demo_agent(self):
        """使用 _DemoAgent (含 degraded_reply) 完整运行套件"""
        from chaos.run_bench import _DemoAgent, _DemoLLMClient
        agent = _DemoAgent()
        client = _DemoLLMClient()
        fault_client = FaultInjectingLLMClient(client)
        bench = ReliabilityBench(
            agent=agent, fault_client=fault_client,
            single_timeout_prob=1.0, single_timeout_requests=3,
            burst_errors_count=2,
            slow_response_ms=10,
            partial_failure_prob=1.0, partial_failure_requests=3,
            cascading_failure_prob=1.0, cascading_failure_requests=3,
            recovery_test_fault_count=2,
            sustained_load_prob=1.0, sustained_load_requests=5,
        )
        report = await bench.run_suite()
        assert len(report.scenario_results) == 7
        # 全部场景应通过 (DemoAgent 提供 degraded_reply)
        passed_count = sum(1 for r in report.scenario_results if r.passed)
        assert passed_count >= 5  # 至少 5 个通过 (允许个别场景失败)


# ============================================================
# 8. CLI 入口
# ============================================================

class TestCLI:
    """命令行入口"""

    def test_cli_module_importable(self):
        """chaos.run_bench 模块可导入"""
        from chaos import run_bench
        assert hasattr(run_bench, "main")
        assert hasattr(run_bench, "print_report")
        assert hasattr(run_bench, "_DemoAgent")
        assert hasattr(run_bench, "_DemoLLMClient")

    def test_print_report_does_not_raise(self, mock_agent, fault_client):
        """print_report 不抛出"""
        from chaos.run_bench import print_report
        bench = ReliabilityBench(agent=mock_agent, fault_client=fault_client)
        report = BenchReport(scenario_results=[
            ScenarioResult(
                name="s1", passed=True,
                faults_injected=1, faults_recovered=1,
                degradation_triggered=True,
                recovery_time=0.01,
            ),
        ])
        bench.compute_scores(report)
        bench.generate_recommendations(report)
        # 不抛出即通过
        print_report(report)
