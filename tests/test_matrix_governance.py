"""矩阵治理闭环测试套件 — L2 Golden Dataset + L3 自动优化器 + L4 A/B 测试 + L5 效果验证 + L6 LLM-as-Judge.

测试 6 轮:
  L2: Golden Dataset 完整性 — 30 case, 10 场景 × 3, 字段齐全
  L3: 自动优化器 — dry-run / 快照 / 回滚 / diff 计算
  L4: A/B 测试 — matched pairs / bootstrap CI / shadow→canary
  L5: 效果验证 — 4 指标 / 回滚阈值 / 自动回滚
  E2E: 完整闭环 — optimize_and_validate 入口
  L6: LLM-as-Judge 注入 — mock router 真实调用路径

Run:
  python -m pytest tests/test_matrix_governance.py -v
  python tests/test_matrix_governance.py
"""
from __future__ import annotations

import os
import sys
import time
import asyncio
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.asyncio


async def test_l2_golden_dataset():
    """L2: Golden Dataset 完整性验证."""
    from memory.matrix_governance import (
        get_golden_dataset, get_golden_case, LLM_JUDGE_RUBRIC,
    )

    print("\n=== L2: Golden Dataset 完整性 ===")

    # 1. 数量验证: 30 case
    dataset = get_golden_dataset()
    assert len(dataset) == 30, f"Golden Dataset 应为 30 case, 实际 {len(dataset)}"
    print(f"  case 数量: {len(dataset)} ✓")

    # 2. 场景覆盖: 10 场景 × 3 case
    scenes = set(c.scene for c in dataset)
    expected_scenes = {
        "greeting", "emotional", "time", "identity", "task",
        "tool", "debug", "creative", "learning", "default"
    }
    assert scenes == expected_scenes, f"场景覆盖不全: {scenes ^ expected_scenes}"
    print(f"  场景覆盖: {len(scenes)} 个 ✓")

    # 3. 每场景 3 case
    from collections import Counter
    scene_counts = Counter(c.scene for c in dataset)
    for scene, count in scene_counts.items():
        assert count == 3, f"场景 {scene} 应有 3 case, 实际 {count}"
    print("  每场景 3 case ✓")

    # 4. 难度分布: 每场景 easy/medium/hard 各 1
    for scene in expected_scenes:
        difficulties = [c.difficulty for c in dataset if c.scene == scene]
        assert sorted(difficulties) == ["easy", "hard", "medium"], \
            f"场景 {scene} 难度分布异常: {difficulties}"
    print("  难度分布: 每场景 easy/medium/hard 各 1 ✓")

    # 5. 字段完整性
    for c in dataset:
        assert c.case_id, f"case_id 为空: {c}"
        assert c.input, f"input 为空: {c}"
        assert c.expected_priority_tail in (
            "USER.md", "AGENTS.md", "MEMORY.md", "HEARTBEAT.md"
        ), f"case {c.case_id} 期望末尾模块不在 Scene-Aware Middle: {c.expected_priority_tail}"
        assert c.difficulty in ("easy", "medium", "hard"), f"难度值异常: {c.difficulty}"
    print("  字段完整性 ✓")

    # 6. case_id 唯一
    case_ids = [c.case_id for c in dataset]
    assert len(set(case_ids)) == 30, "case_id 有重复"
    print("  case_id 唯一 ✓")

    # 7. get_golden_case 按 ID 查询
    case = get_golden_case("greeting_1")
    assert case is not None
    assert case.scene == "greeting"
    assert get_golden_case("nonexistent") is None
    print("  按 ID 查询 ✓")

    # 8. LLM-as-Judge rubric 存在且含关键占位符
    assert "{user_input}" in LLM_JUDGE_RUBRIC
    assert "{reference_answer}" in LLM_JUDGE_RUBRIC
    assert "{response}" in LLM_JUDGE_RUBRIC
    assert "5 分" in LLM_JUDGE_RUBRIC  # GEM 2026: 最高分描述
    assert "1 分" in LLM_JUDGE_RUBRIC  # GEM 2026: 最低分描述
    print("  LLM-as-Judge rubric 完整 ✓")

    print("  L2 PASS")
    return {"n_cases": len(dataset), "n_scenes": len(scenes)}


async def test_l3_auto_optimizer():
    """L3: 自动优化器 — dry-run / 快照 / 回滚 / diff 计算."""
    import copy
    import prompt_builder
    from memory.matrix_governance import (
        auto_optimize_matrix, save_snapshot, rollback_snapshot, list_snapshots,
        compute_matrix_diff,
    )

    print("\n=== L3: 自动优化器 ===")

    # 保存原始矩阵 (测试后恢复)
    original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)

    try:
        # 1. dry-run (默认, 不修改矩阵)
        dry_result = auto_optimize_matrix(dry_run=True)
        assert "diffs" in dry_result
        assert "diff_count" in dry_result
        assert dry_result["applied"] is False
        print(f"  dry-run diff_count: {dry_result['diff_count']} ✓")

        # 2. 不设环境变量时, auto_apply 应被强制为 dry-run
        os.environ.pop("PROMPT_MATRIX_AUTO_APPLY", None)
        forced_dry = auto_optimize_matrix(dry_run=False)
        assert forced_dry["applied"] is False, "未设环境变量不应实际应用"
        print("  环境变量门控 ✓")

        # 3. 快照机制
        snap_count_before = len(list_snapshots())
        snap = save_snapshot(reason="test_l3")
        snap_count_after = len(list_snapshots())
        assert snap_count_after == snap_count_before + 1
        assert snap.snapshot_id.startswith("snap_")
        assert snap.reason == "test_l3"
        print(f"  快照保存: {snap.snapshot_id} ✓")

        # 4. 矩阵 diff 计算
        old_matrix = {
            "AGENTS.md": {"default": 5, "task": 8},
            "USER.md": {"default": 6, "task": 4},
        }
        new_matrix = {
            "AGENTS.md": {"default": 5, "task": 9},  # task 改了
            "USER.md": {"default": 6, "task": 4},     # 没改
            "MEMORY.md": {"default": 3},               # 新增
        }
        diffs = compute_matrix_diff(old_matrix, new_matrix)
        assert len(diffs) == 2, f"应检测到 2 处 diff, 实际 {len(diffs)}"
        diff_strs = [f"{d['module']}[{d['scene']}]: {d['old']}→{d['new']}" for d in diffs]
        print(f"  diff 计算: {diff_strs} ✓")

        # 5. 回滚机制 (回滚到刚保存的快照)
        rolled_back = rollback_snapshot(snap.snapshot_id)
        assert rolled_back is True, "回滚应成功"
        print("  回滚成功 ✓")

        # 6. 回滚不存在的快照
        rolled_back_fake = rollback_snapshot("nonexistent_snap")
        assert rolled_back_fake is False
        print("  回滚不存在快照: 返回 False ✓")

        # 7. 列出快照
        snaps = list_snapshots()
        assert isinstance(snaps, list)
        assert all("snapshot_id" in s for s in snaps)
        print(f"  快照列表: {len(snaps)} 个 ✓")
    finally:
        # 恢复原始矩阵 (避免污染其他测试)
        prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

    print("  L3 PASS")
    return {"dry_run_diffs": dry_result["diff_count"]}


async def test_l4_ab_test():
    """L4: A/B 测试 — matched pairs / bootstrap CI / shadow 模式."""
    import copy
    import prompt_builder
    from memory.matrix_governance import (
        ABTestRunner, _bootstrap_ci, _matched_pair_test, ABTestReport,
    )

    print("\n=== L4: A/B 测试 ===")

    # 保存原始矩阵 (测试后恢复)
    original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)

    try:
        # 1. bootstrap CI 基础测试
        deltas = [0.1, 0.2, 0.15, 0.05, 0.25, 0.1, 0.2, 0.3, 0.1, 0.15]
        ci_low, ci_high = _bootstrap_ci(deltas, n_resamples=1000)
        assert ci_low < ci_high, "CI 下界应小于上界"
        assert ci_low > 0, f"delta 都为正, CI 下界应 > 0, 实际 {ci_low}"
        print(f"  bootstrap CI: [{ci_low:.3f}, {ci_high:.3f}] ✓")

        # 2. CI 跨 0 (不显著)
        deltas_cross_zero = [-0.1, 0.1, -0.05, 0.05, 0.0, -0.1, 0.1, -0.05, 0.05, 0.0]
        ci_low2, ci_high2 = _bootstrap_ci(deltas_cross_zero, n_resamples=1000)
        assert ci_low2 <= 0 <= ci_high2, f"CI 应跨 0, 实际 [{ci_low2}, {ci_high2}]"
        print(f"  CI 跨 0 检测: [{ci_low2:.3f}, {ci_high2:.3f}] ✓")

        # 3. matched pair test
        scores_a = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0]  # 70% 准确率
        scores_b = [1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # 90% 准确率
        stats = _matched_pair_test(scores_a, scores_b)
        assert stats["delta_mean"] > 0, "B 应优于 A"
        assert "is_significant" in stats
        assert "p_value" in stats
        print(f"  matched pair: delta={stats['delta_mean']:.3f}, "
              f"CI=[{stats['ci_low']:.3f}, {stats['ci_high']:.3f}], "
              f"sig={stats['is_significant']} ✓")

        # 4. shadow A/B 测试 (离线全量, 0 LLM 调用)
        runner = ABTestRunner(mode="shadow")
        report = runner.run_shadow()
        assert isinstance(report, ABTestReport)
        assert report.mode == "shadow"
        assert report.n_cases == 30
        assert 0 <= report.a_accuracy <= 1
        assert 0 <= report.b_accuracy <= 1
        assert -1 <= report.accuracy_delta <= 1
        assert report.recommendation in ("ship", "rollback", "inconclusive")
        print(f"  shadow A/B: n={report.n_cases}, "
              f"A_acc={report.a_accuracy:.3f}, B_acc={report.b_accuracy:.3f}, "
              f"Δ={report.accuracy_delta:+.3f}, CI=[{report.bootstrap_ci_low:.3f}, "
              f"{report.bootstrap_ci_high:.3f}], 决策={report.recommendation} ✓")

        # 5. 报告序列化
        report_dict = report.to_dict()
        assert "timestamp" in report_dict
        assert "mode" in report_dict
        assert "per_case" in report_dict
        assert len(report_dict["per_case"]) == 30
        print("  报告序列化: 30 case 详情 ✓")

        # 6. canary 模式 (含 LLM-as-Judge 占位)
        runner_canary = ABTestRunner(mode="canary")
        report_canary = runner_canary.run_canary()
        assert report_canary.mode == "canary"
        print("  canary 模式 ✓")

        # 7. run_full_eval 入口 (自动决定模式)
        runner_full = ABTestRunner(mode="shadow")
        report_full = runner_full.run_full_eval()
        assert report_full.recommendation in ("ship", "rollback", "inconclusive")
        print(f"  run_full_eval: {report_full.recommendation} ✓")
    finally:
        # 恢复原始矩阵 (避免污染其他测试)
        prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

    print("  L4 PASS")
    return {
        "a_accuracy": report.a_accuracy,
        "b_accuracy": report.b_accuracy,
        "delta": report.accuracy_delta,
        "recommendation": report.recommendation,
    }


async def test_l5_health_evaluation():
    """L5: 效果验证 — 4 指标 / 回滚阈值 / 自动回滚."""
    import copy
    import prompt_builder
    from memory.matrix_governance import (
        evaluate_matrix_health, capture_baseline, rollback_if_degraded,
        ROLLBACK_THRESHOLDS, MatrixHealthReport,
    )

    print("\n=== L5: 效果验证 ===")

    # 保存原始矩阵 (测试后恢复, 避免污染其他测试)
    original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)

    try:
        # 1. 基线捕获
        baseline = capture_baseline()
        assert "scene_accuracy" in baseline
        assert "complexity_alignment" in baseline
        assert "cache_hit_rate" in baseline
        assert "priority_match_rate" in baseline
        print(f"  基线: scene_acc={baseline['scene_accuracy']:.3f}, "
              f"alignment={baseline['complexity_alignment']:.3f}, "
              f"cache={baseline['cache_hit_rate']:.3f}, "
              f"priority={baseline['priority_match_rate']:.3f} ✓")

        # 2. 健康评估
        health = evaluate_matrix_health()
        assert isinstance(health, MatrixHealthReport)
        assert 0 <= health.scene_accuracy <= 1
        assert 0 <= health.complexity_alignment <= 1
        assert 0 <= health.cache_hit_rate <= 1
        assert 0 <= health.priority_match_rate <= 1
        assert health.n_cases == 30
        print(f"  健康: scene_acc={health.scene_accuracy:.3f}, "
              f"alignment={health.complexity_alignment:.3f}, "
              f"cache={health.cache_hit_rate:.3f}, "
              f"priority={health.priority_match_rate:.3f} ✓")

        # 3. 回滚阈值存在
        assert "scene_accuracy_drop" in ROLLBACK_THRESHOLDS
        assert "cache_hit_rate_drop" in ROLLBACK_THRESHOLDS
        assert "priority_match_drop" in ROLLBACK_THRESHOLDS
        print(f"  回滚阈值: {ROLLBACK_THRESHOLDS} ✓")

        # 4. 无退化时不回滚
        rolled_back = rollback_if_degraded(baseline)
        assert rolled_back is False, "无退化时不应回滚"
        print("  无退化不回滚 ✓")

        # 5. 退化时回滚 (构造退化基线)
        degraded_baseline = {
            "scene_accuracy": baseline["scene_accuracy"] + 0.5,  # 比当前高 50%
            "cache_hit_rate": baseline["cache_hit_rate"] + 0.5,
            "priority_match_rate": baseline["priority_match_rate"] + 0.5,
        }
        # 先保存快照 (回滚需要快照)
        from memory.matrix_governance import save_snapshot
        save_snapshot(reason="test_l5_degraded")
        rolled_back_degraded = rollback_if_degraded(degraded_baseline)
        assert rolled_back_degraded is True, "退化时应回滚"
        print("  退化时自动回滚 ✓")

        # 6. 健康报告序列化
        health_dict = health.to_dict()
        assert "should_rollback" in health_dict
        assert "rollback_reasons" in health_dict
        print("  健康报告序列化 ✓")
    finally:
        # 恢复原始矩阵 (避免污染其他测试)
        prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

    print("  L5 PASS")
    return {
        "scene_accuracy": health.scene_accuracy,
        "complexity_alignment": health.complexity_alignment,
        "should_rollback": health.should_rollback,
    }


async def test_e2e_full_loop():
    """E2E: 完整闭环 — optimize_and_validate 入口."""
    import copy
    import prompt_builder
    from memory.matrix_governance import optimize_and_validate

    print("\n=== E2E: 完整闭环 ===")

    # 保存原始矩阵 (测试后恢复, 避免污染其他测试)
    original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)

    try:
        # 1. 仅 dry-run (不自动应用)
        result = optimize_and_validate(auto_apply=False, full_eval=False)
        assert "baseline" in result
        assert "diff_count" in result
        assert result["applied"] is False
        assert result["ab_test"] is None
        assert result["rolled_back"] is False
        print(f"  dry-run 模式: diff_count={result['diff_count']} ✓")

        # 2. auto_apply=True 但无环境变量 (应被强制为 dry-run)
        os.environ.pop("PROMPT_MATRIX_AUTO_APPLY", None)
        result_no_env = optimize_and_validate(auto_apply=True, full_eval=False)
        assert result_no_env["applied"] is False, "无环境变量不应应用"
        print("  无环境变量: 强制 dry-run ✓")

        # 3. auto_apply=True + 环境变量 + full_eval
        os.environ["PROMPT_MATRIX_AUTO_APPLY"] = "1"
        try:
            result_full = optimize_and_validate(auto_apply=True, full_eval=True)
            assert "baseline" in result_full
            assert "diff_count" in result_full
            assert "ab_test" in result_full
            assert "health" in result_full
            print(f"  完整闭环: applied={result_full['applied']}, "
                  f"rolled_back={result_full['rolled_back']} ✓")
        finally:
            os.environ.pop("PROMPT_MATRIX_AUTO_APPLY", None)
    finally:
        # 恢复原始矩阵 (避免污染其他测试)
        prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

    print("  E2E PASS")
    return {"full_loop_completed": True}


async def test_l6_llm_judge_injection():
    """L6: LLM-as-Judge 注入测试 — mock router 真实调用路径.

    验证:
      1. 无 router 时返回占位 (向后兼容)
      2. 注入 mock router 后真实调用 route() 2 次/case (生成回复 + 评分)
      3. _parse_judge_score 能解析各种格式
      4. _decide_with_judge 决策矩阵正确
      5. report.llm_judge_avg_score 字段被填充
    """
    import copy
    import prompt_builder
    from memory.matrix_governance import (
        ABTestRunner, ABTestReport,
    )

    print("\n=== L6: LLM-as-Judge 注入 ===")

    original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)

    try:
        # 1. 无 router 时返回占位
        runner_no_router = ABTestRunner(mode="canary")
        assert runner_no_router.router is None
        placeholder = runner_no_router._run_llm_judge_subset(original_matrix)
        assert placeholder["scores"] == []
        assert placeholder["avg_score"] == 0.0
        assert "占位" in placeholder["note"]
        print(f"  无 router 占位 ✓: {placeholder['note']}")

        # 2. _parse_judge_score 各种格式
        parse = ABTestRunner._parse_judge_score
        assert parse('{"score": 5, "reason": "好"}') == 5.0
        assert parse('{"score": 1, "reason": "差"}') == 1.0
        assert parse('评分是 4 分') == 4.0
        assert parse('评分 3') == 3.0
        assert parse('no score here') == 0.0
        assert parse('{"reason": "x"}') == 0.0  # 无 score 字段
        print("  _parse_judge_score 多格式解析 ✓")

        # 3. 注入 mock router
        class MockRouter:
            """Mock router: 第 1 次调用返回回复, 第 2 次返回评分 JSON."""
            def __init__(self):
                self.call_count = 0
                self.call_history = []

            async def route(self, task_type, messages, temperature=0.7,
                           max_tokens=None, timeout=None, **kwargs):
                self.call_count += 1
                self.call_history.append({
                    "task_type": task_type,
                    "n_messages": len(messages),
                    "temperature": temperature,
                })
                # 根据调用次数判断: 奇数=生成回复, 偶数=评分
                if self.call_count % 2 == 1:
                    # 生成回复 (response_task_type)
                    assert task_type == "chat", f"生成回复 task_type 应为 chat, 实际 {task_type}"
                    return f"这是 case {self.call_count} 的回复内容"
                else:
                    # 评分 (judge_task_type)
                    assert task_type == "chat_flash", f"评分 task_type 应为 chat_flash, 实际 {task_type}"
                    return '{"score": 4, "reason": "回复切题"}'

        mock_router = MockRouter()
        runner = ABTestRunner(
            mode="canary",
            router=mock_router,
            judge_task_type="chat_flash",
            response_task_type="chat",
        )
        assert runner.router is mock_router

        # 跑 canary (会触发 LLM-as-Judge)
        report = runner.run_canary()
        assert isinstance(report, ABTestReport)
        assert report.mode == "canary"

        # 10 个 case × 2 次调用 = 20 次
        assert mock_router.call_count == 20, f"应调用 20 次, 实际 {mock_router.call_count}"
        print(f"  mock router 调用次数: {mock_router.call_count} (10 case × 2) ✓")

        # LLM-as-Judge 字段被填充
        assert report.llm_judge_n_cases == 10
        assert report.llm_judge_avg_score == 4.0, f"平均分应为 4.0, 实际 {report.llm_judge_avg_score}"
        assert "LLM-as-Judge 完成" in report.llm_judge_note
        print(f"  LLM-as-Judge 评分: avg={report.llm_judge_avg_score}, "
              f"n={report.llm_judge_n_cases} ✓")

        # 决策: shadow 不显著 + LLM 4.0 → ship
        assert report.recommendation == "ship", f"应 ship, 实际 {report.recommendation}"
        print(f"  决策: {report.recommendation} (shadow 不显著 + LLM 4.0) ✓")

        # 4. _decide_with_judge 决策矩阵
        shadow_report = ABTestReport(
            timestamp=time.time(), mode="shadow", n_cases=30,
            is_significant=False,  # shadow 不显著
        )
        # LLM 4.5 → ship
        assert runner._decide_with_judge(shadow_report, {"scores": [4.5], "avg_score": 4.5}) == "ship"
        # LLM 1.5 → rollback
        assert runner._decide_with_judge(shadow_report, {"scores": [1.5], "avg_score": 1.5}) == "rollback"
        # LLM 3.0 → inconclusive
        assert runner._decide_with_judge(shadow_report, {"scores": [3.0], "avg_score": 3.0}) == "inconclusive"
        # 无 scores → 退回 shadow
        assert runner._decide_with_judge(shadow_report, {"scores": []}) == shadow_report.recommendation
        print("  决策矩阵: >=4.0 ship / <=2.0 rollback / (2,4) inconclusive ✓")

        # 5. shadow 显著时直接用 shadow 决策 (忽略 LLM)
        sig_report = ABTestReport(
            timestamp=time.time(), mode="shadow", n_cases=30,
            is_significant=True, recommendation="rollback",
        )
        assert runner._decide_with_judge(sig_report, {"scores": [4.5], "avg_score": 4.5}) == "rollback"
        print("  shadow 显著时覆盖 LLM 决策 ✓")

        # 6. report.to_dict 含 LLM 字段
        report_dict = report.to_dict()
        assert "llm_judge_avg_score" in report_dict
        assert "llm_judge_n_cases" in report_dict
        assert "llm_judge_note" in report_dict
        print("  报告序列化含 LLM 字段 ✓")

    finally:
        prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

    print("  L6 PASS")
    return {
        "mock_calls": mock_router.call_count,
        "avg_score": report.llm_judge_avg_score,
        "recommendation": report.recommendation,
    }


# ============================================================================
# 主入口
# ============================================================================

async def main():
    """运行所有测试."""
    print("\n" + "=" * 60)
    print("  矩阵治理闭环测试 (L2 + L3 + L4 + L5 + E2E + L6)")
    print("=" * 60)

    l2 = await test_l2_golden_dataset()
    l3 = await test_l3_auto_optimizer()
    l4 = await test_l4_ab_test()
    l5 = await test_l5_health_evaluation()
    e2e = await test_e2e_full_loop()
    l6 = await test_l6_llm_judge_injection()

    print("\n" + "=" * 60)
    print("  所有测试通过 ✓")
    print("=" * 60)
    print(f"  L2 Golden Dataset: {l2['n_cases']} case, {l2['n_scenes']} 场景")
    print(f"  L3 自动优化器: dry-run {l3['dry_run_diffs']} 处 diff")
    print(f"  L4 A/B 测试: A={l4['a_accuracy']:.3f}, B={l4['b_accuracy']:.3f}, "
          f"Δ={l4['delta']:+.3f}, 决策={l4['recommendation']}")
    print(f"  L5 效果验证: scene_acc={l5['scene_accuracy']:.3f}, "
          f"alignment={l5['complexity_alignment']:.3f}, "
          f"should_rollback={l5['should_rollback']}")
    print(f"  E2E 完整闭环: {e2e['full_loop_completed']}")
    print(f"  L6 LLM-as-Judge 注入: {l6['mock_calls']} 次调用, "
          f"avg={l6['avg_score']}, 决策={l6['recommendation']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
