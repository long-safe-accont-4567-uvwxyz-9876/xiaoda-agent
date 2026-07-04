"""提示词矩阵治理闭环 — 检测 → 自动优化 → A/B 测试 → 效果验证 → 反馈调整。

5 层架构 (用户已确认 L2+L3+L4 完整闭环):
  L1 检测层:    prompt_complexity.analyze_scene_complexity_alignment (已有, 不修改)
  L2 Golden Dataset: 30 个 case (10 场景 × 3) + LLM-as-Judge rubric
  L3 自动优化器:  apply_recommendation_safe + 快照回滚 + dry-run
  L4 A/B 测试:   matched pairs + bootstrap CI + shadow→canary→ramp
  L5 效果验证:   4 量化指标 + 自动回滚阈值

学术支撑:
  - LLM-as-Judge (GEM 2026, Yamauchi et al.):
      * 提供 reference answer + score descriptions 至关重要
      * 只为最高/最低分提供 description 最可靠
      * sampling 解码 > greedy 对齐人类判断
      * rubric 清晰时 CoT 增益微小, CoT-free + score averaging 最划算
  - A/B Testing LLM Prompts 2026 (FutureAGI):
      * n_per_arm = 16 * σ² / MDE²
      * matched pairs (同输入跑 A/B) → variance 减少 1-2 个数量级
      * bootstrap CI 10000 次重采样在 delta 上 (不是 aggregate)
      * shadow → canary (1-5%) → ramp (25%) → 100%, eval-gated rollback
  - GEPA/DSPy (Agrawal et al., ICLR 2026 oral):
      * 反射式 prompt 进化, metric 返回 score + 自然语言 feedback
      * 35x fewer rollouts than GRPO, +14% 比 MIPROv2
      * Pareto frontier 保留多样策略
  - Hecate (arXiv:2607.01903v1):
      * 结构广度 > 体积, 高复杂度模块应靠近用户输入

设计原则 (降本增效):
  - 主指标: 场景识别准确率 + 复杂度对齐率 (纯本地, 0 LLM 调用)
  - 副指标: 缓存命中率 (已有 _scene_cache_hits/_scene_cache_misses)
  - 质量指标: LLM-as-Judge 仅在 canary 阶段调用 (控制成本)
  - bootstrap CI: 10000 次重采样 (numpy 纯本地, 0 LLM 调用)

用法:
  # L3 自动优化器
  from memory.matrix_governance import auto_optimize_matrix
  result = auto_optimize_matrix(dry_run=True)  # 先 dry-run 看 diff
  result = auto_optimize_matrix(dry_run=False)  # 应用并保存快照

  # L4 A/B 测试
  from memory.matrix_governance import ABTestRunner
  runner = ABTestRunner()
  report = runner.run_full_eval()  # shadow 模式全量评估

  # L5 效果验证
  from memory.matrix_governance import evaluate_matrix_health
  health = evaluate_matrix_health()
  if health.should_rollback:
      rollback_matrix()
"""
from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# ── 轻量模式 Feature Flag ──
# 设置 FEATURE_GOVERNANCE_LIGHT=1 可跳过 L4 A/B 测试和 L6 LLM-as-Judge,
# 仅保留 L2 Golden Dataset + L3 优化器 + L5 效果验证 (纯本地, 0 LLM 调用)
# 适用于: 边缘端/嵌入式/轻量客户端/CI 快速验证
GOVERNANCE_LIGHTWEIGHT = os.environ.get("FEATURE_GOVERNANCE_LIGHT", "").strip() in ("1", "true", "yes")


# ============================================================================
# L2: Golden Dataset — 30 case (10 场景 × 3) + LLM-as-Judge rubric
# ============================================================================

@dataclass
class GoldenCase:
    """单个 Golden 测试用例.

    设计依据 (GEM 2026 LLM-as-Judge):
      - input: 真实用户输入 (覆盖口语化/正式/混合)
      - expected_scene: 期望场景识别结果 (主指标, 纯本地)
      - expected_priority_tail: 期望排序末尾的模块 (验证矩阵功能性)
      - reference_keywords: 期望回复中应包含的关键词 (轻量质量指标)
      - reference_answer: 参考答案 (LLM-as-Judge 用, 仅 canary 阶段调用)
      - difficulty: easy/medium/hard (用于分层统计)
    """
    case_id: str
    scene: str           # 期望场景
    input: str           # 用户输入
    expected_priority_tail: str   # 期望 Scene-Aware Middle 末尾模块
    reference_keywords: list[str] # 期望回复关键词
    reference_answer: str = ""    # 参考答案 (LLM-as-Judge 用)
    difficulty: str = "medium"    # easy/medium/hard


# 30 个 Golden Case — 10 场景 × 3 case (easy/medium/hard 各 1)
# 设计原则:
#   - 覆盖三层架构意图识别 (正则/关键词/置信度)
#   - 覆盖口语化归一化 (咋办/啥意思/为啥)
#   - 覆盖否定隔离 (不要难过/别帮我)
#   - 覆盖多场景混合 (好累啊查天气)
#   - expected_priority_tail 基于 prompt_builder._MODULE_SCENE_PRIORITY
GOLDEN_DATASET: list[GoldenCase] = [
    # ── greeting (3 case) — emotion_bucket, USER.md 末尾 ──
    GoldenCase("greeting_1", "greeting", "早上好呀",
               "USER.md", ["早安", "早上", "好"], difficulty="easy"),
    GoldenCase("greeting_2", "greeting", "嗨,在吗",
               "USER.md", ["在", "陪你"], difficulty="medium"),
    GoldenCase("greeting_3", "greeting", "回来啦,我回家了",
               "USER.md", ["欢迎", "回来"], difficulty="hard"),

    # ── emotional (3 case) — emotion_bucket, USER.md 末尾 ──
    GoldenCase("emotional_1", "emotional", "今天好难过啊",
               "USER.md", ["难过", "陪", "抱抱"], difficulty="easy"),
    GoldenCase("emotional_2", "emotional", "压力好大,快崩溃了",
               "USER.md", ["压力", "崩溃", "陪"], difficulty="medium"),
    GoldenCase("emotional_3", "emotional", "不要难过,这不算啥",  # 否定隔离
               "USER.md", [], difficulty="hard"),

    # ── time (3 case) — emotion_bucket (S级), S级立刻重排 ──
    # expected: AGENTS.md[time]=4 最高 (Scene-Aware Middle), SOUL.md 在 Stable Prefix
    GoldenCase("time_1", "time", "几点了？",
               "AGENTS.md", ["时间", "点"], difficulty="easy"),
    GoldenCase("time_2", "time", "今天星期几",
               "AGENTS.md", ["星期", "周"], difficulty="medium"),
    GoldenCase("time_3", "time", "现在是什么时候",
               "AGENTS.md", ["时间", "时候"], difficulty="hard"),

    # ── identity (3 case) — cognition_bucket (S级) ──
    # expected: USER.md[identity]=5 最高 (Scene-Aware Middle), IDENTITY.md 在 Stable Prefix
    GoldenCase("identity_1", "identity", "你是谁",
               "USER.md", ["纳西妲", "草神", "须弥"], difficulty="easy"),
    GoldenCase("identity_2", "identity", "你叫什么名字",
               "USER.md", ["名字", "纳西妲"], difficulty="medium"),
    GoldenCase("identity_3", "identity", "介绍一下你自己",
               "USER.md", ["纳西妲", "自我", "介绍"], difficulty="hard"),

    # ── task (3 case) — function_bucket, AGENTS.md 末尾 ──
    GoldenCase("task_1", "task", "帮我写个脚本",
               "AGENTS.md", ["脚本", "写", "帮"], difficulty="easy"),
    GoldenCase("task_2", "task", "咋整啊,这个报错",  # 口语化归一化
               "AGENTS.md", ["报错", "整"], difficulty="hard"),
    GoldenCase("task_3", "task", "能不能帮我部署一下",
               "AGENTS.md", ["部署", "帮"], difficulty="medium"),

    # ── tool (3 case) — function_bucket, AGENTS.md 末尾 ──
    GoldenCase("tool_1", "tool", "查一下今天天气",
               "AGENTS.md", ["天气", "查"], difficulty="easy"),
    GoldenCase("tool_2", "tool", "提醒我三点开会",
               "AGENTS.md", ["提醒", "三点", "会"], difficulty="medium"),
    GoldenCase("tool_3", "tool", "翻译一下这句话",
               "AGENTS.md", ["翻译", "译"], difficulty="hard"),

    # ── debug (3 case) — function_bucket, HEARTBEAT.md 末尾 ──
    GoldenCase("debug_1", "debug", "报错了,跑不起来",
               "HEARTBEAT.md", ["报错", "跑"], difficulty="easy"),
    GoldenCase("debug_2", "debug", "异常: FileNotFoundError",
               "HEARTBEAT.md", ["异常", "File"], difficulty="medium"),
    GoldenCase("debug_3", "debug", "为什么失败了,排查一下",
               "HEARTBEAT.md", ["失败", "排查"], difficulty="hard"),

    # ── creative (3 case) — emotion_bucket, USER.md 末尾 ──
    GoldenCase("creative_1", "creative", "写首诗",
               "USER.md", ["诗", "写"], difficulty="easy"),
    GoldenCase("creative_2", "creative", "画一张风景图",
               "USER.md", ["画", "图"], difficulty="medium"),
    GoldenCase("creative_3", "creative", "想个点子,起个名字",
               "USER.md", ["点子", "名字"], difficulty="hard"),

    # ── learning (3 case) — cognition_bucket, USER.md 末尾 ──
    # expected: USER.md[learning]=6 最高 (个性化教学)
    GoldenCase("learning_1", "learning", "什么是递归",
               "USER.md", ["递归", "解释"], difficulty="easy"),
    GoldenCase("learning_2", "learning", "啥意思,讲讲",  # 口语化归一化
               "USER.md", ["意思", "讲"], difficulty="medium"),
    GoldenCase("learning_3", "learning", "为什么天空是蓝色的",
               "USER.md", ["为什么", "天空", "蓝"], difficulty="hard"),

    # ── default (3 case) — default_bucket, USER.md 末尾 ──
    # expected: USER.md[default]=6 最高 (通用排序)
    GoldenCase("default_1", "default", "random xyz 12345",
               "USER.md", [], difficulty="easy"),
    GoldenCase("default_2", "default", "嗯嗯好的",
               "USER.md", [], difficulty="medium"),
    GoldenCase("default_3", "default", "。。。",  # 极低质量闲聊, 粘性阈值
               "USER.md", [], difficulty="hard"),
]


# LLM-as-Judge rubric — 1-5 评分 (GEM 2026: 只为最高/最低分提供 description)
LLM_JUDGE_RUBRIC = """
你是提示词矩阵质量评估员。请对以下回复打分 (1-5 分)。

评分标准 (GEM 2026: 只描述最高分和最低分):
  5 分: 回复完全切题, 语气符合纳西妲人设 (温柔/好奇/比喻丰富),
        关键信息准确, 无幻觉, 长度适中
  3 分: 回复基本切题, 但语气或内容有轻微偏差
  1 分: 回复跑题, 语气不符, 含幻觉, 或完全无意义

输入: {user_input}
参考答案: {reference_answer}
待评估回复: {response}

请只返回一个 JSON: {{"score": <1-5>, "reason": "<一句话理由>"}}
"""


def get_golden_dataset() -> list[GoldenCase]:
    """获取 Golden Dataset (30 case)."""
    return list(GOLDEN_DATASET)


def get_golden_case(case_id: str) -> GoldenCase | None:
    """按 ID 获取单个 case."""
    for c in GOLDEN_DATASET:
        if c.case_id == case_id:
            return c
    return None


# ============================================================================
# L3: 自动优化器 — apply_recommendation_safe + 快照回滚
# ============================================================================

@dataclass
class MatrixSnapshot:
    """矩阵快照 (用于回滚)."""
    timestamp: float
    matrix: dict[str, dict[str, int]]
    reason: str  # 快照原因 (manual/auto_optimize/before_ab_test)
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            self.snapshot_id = f"snap_{int(self.timestamp * 1000)}"


# 快照存储 (内存 + 可选持久化)
_snapshots: list[MatrixSnapshot] = []
_MAX_SNAPSHOTS = 10


def _get_current_matrix() -> dict[str, dict[str, int]]:
    """获取当前生效的优先级矩阵."""
    try:
        import prompt_builder
        return copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)
    except (ImportError, AttributeError):
        return {}


def _set_matrix(new_matrix: dict[str, dict[str, int]]) -> None:
    """设置新的优先级矩阵 (热更新)."""
    import prompt_builder
    prompt_builder._MODULE_SCENE_PRIORITY = copy.deepcopy(new_matrix)
    logger.info("matrix_governance.matrix_updated",
                modules=len(new_matrix),
                scenes=len(next(iter(new_matrix.values()), {})))


def save_snapshot(reason: str = "manual") -> MatrixSnapshot:
    """保存当前矩阵快照 (回滚点)."""
    snapshot = MatrixSnapshot(
        timestamp=time.time(),
        matrix=_get_current_matrix(),
        reason=reason,
    )
    _snapshots.append(snapshot)
    # 保留最近 N 个快照
    while len(_snapshots) > _MAX_SNAPSHOTS:
        _snapshots.pop(0)
    logger.info("matrix_governance.snapshot_saved",
                snapshot_id=snapshot.snapshot_id,
                reason=reason)
    return snapshot


def rollback_snapshot(snapshot_id: str | None = None) -> bool:
    """回滚到指定快照 (不指定则回滚到最近一个)."""
    if not _snapshots:
        logger.warning("matrix_governance.no_snapshot_to_rollback")
        return False

    target = None
    if snapshot_id:
        for s in _snapshots:
            if s.snapshot_id == snapshot_id:
                target = s
                break
    else:
        target = _snapshots[-1]

    if target is None:
        logger.warning("matrix_governance.snapshot_not_found",
                       snapshot_id=snapshot_id)
        return False

    _set_matrix(target.matrix)
    logger.info("matrix_governance.rolled_back",
                snapshot_id=target.snapshot_id,
                reason=target.reason)
    return True


def list_snapshots() -> list[dict]:
    """列出所有快照 (最新的在最后)."""
    return [
        {
            "snapshot_id": s.snapshot_id,
            "timestamp": s.timestamp,
            "reason": s.reason,
            "modules": len(s.matrix),
        }
        for s in _snapshots
    ]


def compute_matrix_diff(
    old: dict[str, dict[str, int]],
    new: dict[str, dict[str, int]],
) -> list[dict]:
    """计算两个矩阵的差异 (用于 dry-run 输出)."""
    diffs = []
    for module in sorted(set(old.keys()) | set(new.keys())):
        old_scenes = old.get(module, {})
        new_scenes = new.get(module, {})
        for scene in sorted(set(old_scenes.keys()) | set(new_scenes.keys())):
            old_val = old_scenes.get(scene)
            new_val = new_scenes.get(scene)
            if old_val != new_val:
                diffs.append({
                    "module": module,
                    "scene": scene,
                    "old": old_val,
                    "new": new_val,
                    "delta": (new_val - old_val) if (old_val is not None and new_val is not None) else None,
                })
    return diffs


def apply_recommendation_safe(
    dry_run: bool = True,
    save_snap: bool = True,
) -> dict:
    """安全应用复杂度对齐推荐 (L3 自动优化器).

    流程:
      1. 从 prompt_complexity 获取推荐矩阵
      2. 计算 diff
      3. dry_run=True: 仅返回 diff, 不修改矩阵
      4. dry_run=False: 保存快照 → 应用新矩阵 → 等待 L4 A/B 测试验证

    Args:
        dry_run: True 仅看 diff, False 实际应用
        save_snap: 应用前是否保存快照 (回滚点)

    Returns:
        {
            "dry_run": bool,
            "diffs": [...],
            "applied": bool,
            "snapshot_id": str | None,
        }
    """
    from memory.prompt_complexity import (
        analyze_scene_complexity_alignment,
        recommend_priority_adjustment,
    )

    # 获取项目根目录
    project_root = Path(__file__).parent.parent
    alignments = analyze_scene_complexity_alignment(project_root)
    recommended = recommend_priority_adjustment(alignments)
    current = _get_current_matrix()

    diffs = compute_matrix_diff(current, recommended)

    result = {
        "dry_run": dry_run,
        "diffs": diffs,
        "diff_count": len(diffs),
        "applied": False,
        "snapshot_id": None,
    }

    if dry_run:
        logger.info("matrix_governance.dry_run", diff_count=len(diffs))
        return result

    # 实际应用: 保存快照 → 应用
    if save_snap:
        snap = save_snapshot(reason="before_auto_optimize")
        result["snapshot_id"] = snap.snapshot_id

    _set_matrix(recommended)
    result["applied"] = True
    logger.info("matrix_governance.applied",
                diff_count=len(diffs),
                snapshot_id=result["snapshot_id"])
    return result


def auto_optimize_matrix(
    dry_run: bool = True,
    env_var: str = "PROMPT_MATRIX_AUTO_APPLY",
) -> dict:
    """自动优化矩阵 (环境变量门控).

    安全边界 (用户确认: 完全自动模式):
      - dry_run=True: 总是先 dry-run
      - dry_run=False: 需要 PROMPT_MATRIX_AUTO_APPLY=1 环境变量
      - 应用前自动保存快照
      - L4 A/B 测试不通过 → 自动回滚 (rollback_if_degraded)

    Args:
        dry_run: 是否仅 dry-run
        env_var: 启用自动应用的环境变量名

    Returns:
        apply_recommendation_safe 的返回值
    """
    if not dry_run:
        # 完全自动模式需要环境变量门控
        if os.environ.get(env_var) != "1":
            logger.warning("matrix_governance.auto_apply_disabled",
                           env_var=env_var,
                           hint=f"设置 {env_var}=1 启用自动应用")
            # 强制 dry-run
            return apply_recommendation_safe(dry_run=True)

    return apply_recommendation_safe(dry_run=dry_run, save_snap=True)


# ============================================================================
# L4: A/B 测试 — matched pairs + bootstrap CI + shadow→canary→ramp
# ============================================================================

@dataclass
class ABTestResult:
    """单个 case 的 A/B 测试结果."""
    case_id: str
    scene: str
    # A 组 (当前矩阵)
    a_scene_correct: bool
    a_priority_tail: str
    a_cache_hit: bool
    # B 组 (候选矩阵)
    b_scene_correct: bool
    b_priority_tail: str
    b_cache_hit: bool
    # 配对 delta (matched pair)
    scene_delta: float = 0.0  # b_correct - a_correct (1/0/-1)
    priority_match_delta: float = 0.0


@dataclass
class ABTestReport:
    """完整 A/B 测试报告."""
    timestamp: float
    mode: str  # shadow / canary / ramp
    n_cases: int
    results: list[ABTestResult] = field(default_factory=list)
    # 主指标: 场景识别准确率
    a_accuracy: float = 0.0
    b_accuracy: float = 0.0
    accuracy_delta: float = 0.0
    # 副指标: 优先级匹配率
    a_priority_match: float = 0.0
    b_priority_match: float = 0.0
    # 副指标: 缓存命中率
    a_cache_hit_rate: float = 0.0
    b_cache_hit_rate: float = 0.0
    # 统计检验
    bootstrap_ci_low: float = 0.0
    bootstrap_ci_high: float = 0.0
    p_value: float = 1.0
    is_significant: bool = False
    # 决策
    recommendation: str = ""  # ship / rollback / inconclusive
    # LLM-as-Judge 评分 (canary 阶段填充, shadow 阶段为 0)
    llm_judge_avg_score: float = 0.0
    llm_judge_n_cases: int = 0
    llm_judge_note: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "mode": self.mode,
            "n_cases": self.n_cases,
            "a_accuracy": round(self.a_accuracy, 4),
            "b_accuracy": round(self.b_accuracy, 4),
            "accuracy_delta": round(self.accuracy_delta, 4),
            "a_priority_match": round(self.a_priority_match, 4),
            "b_priority_match": round(self.b_priority_match, 4),
            "a_cache_hit_rate": round(self.a_cache_hit_rate, 4),
            "b_cache_hit_rate": round(self.b_cache_hit_rate, 4),
            "bootstrap_ci_low": round(self.bootstrap_ci_low, 4),
            "bootstrap_ci_high": round(self.bootstrap_ci_high, 4),
            "p_value": round(self.p_value, 6),
            "is_significant": self.is_significant,
            "recommendation": self.recommendation,
            "llm_judge_avg_score": round(self.llm_judge_avg_score, 4),
            "llm_judge_n_cases": self.llm_judge_n_cases,
            "llm_judge_note": self.llm_judge_note,
            "per_case": [
                {
                    "case_id": r.case_id,
                    "scene": r.scene,
                    "a_scene_correct": r.a_scene_correct,
                    "b_scene_correct": r.b_scene_correct,
                    "scene_delta": r.scene_delta,
                }
                for r in self.results
            ],
        }


def _run_single_case(case: GoldenCase, matrix: dict[str, dict[str, int]]) -> dict:
    """在指定矩阵下运行单个 case (纯本地, 0 LLM 调用).

    Returns:
        {
            "scene_correct": bool,  # 场景识别是否正确
            "priority_tail": str,   # 排序末尾模块
            "cache_hit": bool,      # 缓存是否命中 (模拟)
        }
    """
    try:
        import prompt_builder
        # 临时切换矩阵 (不修改全局)
        original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)
        try:
            prompt_builder._MODULE_SCENE_PRIORITY = matrix
            # 场景识别 (纯本地, 不调用 LLM)
            detected = prompt_builder._classify_scene(case.input)
            scene_correct = (detected == case.scene)

            # 优先级末尾模块 (验证矩阵功能性)
            # matrix 结构: {module_name: {scene_name: priority}}
            # 找到该场景下优先级最高的模块 (靠近用户输入)
            scene = detected  # 复用已识别的场景
            priorities = {m: matrix.get(m, {}).get(scene, 0) for m in matrix}
            priority_tail = max(priorities, key=priorities.get) if priorities else ""

            # 缓存命中模拟: 场景识别正确 + 同场景桶排序结构不变 → 缓存命中
            # 原理: prompt_builder 的场景缓存基于 scene_sig (场景+模块排序),
            # 同场景同矩阵 → scene_sig 相同 → 缓存命中
            if scene_correct:
                # 同场景同矩阵: scene_sig 不变, 缓存命中
                cache_hit = True
            else:
                # 场景变化: scene_sig 改变, 缓存未命中
                cache_hit = False
        finally:
            prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

        return {
            "scene_correct": scene_correct,
            "priority_tail": priority_tail,
            "cache_hit": cache_hit,
        }
    except Exception as e:
        logger.error("matrix_governance.single_case_failed",
                     case_id=case.case_id, error=str(e))
        return {
            "scene_correct": False,
            "priority_tail": "",
            "cache_hit": False,
        }


def _bootstrap_ci(
    deltas: list[float],
    n_resamples: int = 10000,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Bootstrap 置信区间 (在 delta 上重采样, 不是 aggregate).

    学术依据 (A/B Testing LLM Prompts 2026):
      - 10000 次重采样在 delta 上
      - ship only when CI entirely on one side of zero
    """
    if not deltas:
        return (0.0, 0.0)
    try:
        import numpy as np
        arr = np.array(deltas)
        n = len(arr)
        boot_means = np.zeros(n_resamples)
        for i in range(n_resamples):
            sample = np.random.choice(arr, size=n, replace=True)
            boot_means[i] = sample.mean()
        alpha = 1 - confidence
        low = float(np.percentile(boot_means, 100 * alpha / 2))
        high = float(np.percentile(boot_means, 100 * (1 - alpha / 2)))
        return (low, high)
    except ImportError:
        # numpy 不可用时用简单分位数
        import random
        n = len(deltas)
        boot_means = []
        for _ in range(n_resamples):
            sample = [random.choice(deltas) for _ in range(n)]
            boot_means.append(sum(sample) / n)
        boot_means.sort()
        alpha = 1 - confidence
        low_idx = int(n_resamples * alpha / 2)
        high_idx = int(n_resamples * (1 - alpha / 2))
        return (boot_means[low_idx], boot_means[high_idx])


def _matched_pair_test(scores_a: list[float], scores_b: list[float]) -> dict:
    """Matched pair 统计检验 (同输入跑 A/B, 计算 per-case delta).

    学术依据 (FutureAGI 2026):
      - matched pairs: variance 减少 1-2 个数量级
      - bootstrap CI on delta, not aggregate
      - ship only when CI entirely on one side of zero

    Returns:
        {
            "delta_mean": float,
            "ci_low": float,
            "ci_high": float,
            "is_significant": bool,  # CI 不跨 0
            "p_value": float,  # 近似 p-value
        }
    """
    n = min(len(scores_a), len(scores_b))
    deltas = [scores_b[i] - scores_a[i] for i in range(n)]
    if not deltas:
        return {"delta_mean": 0.0, "ci_low": 0.0, "ci_high": 0.0,
                "is_significant": False, "p_value": 1.0}

    delta_mean = sum(deltas) / n
    ci_low, ci_high = _bootstrap_ci(deltas)

    # 显著性: CI 完全在 0 的某一侧
    is_significant = (ci_low > 0 and ci_high > 0) or (ci_low < 0 and ci_high < 0)

    # 近似 p-value (基于 delta 均值与 0 的距离, 用 bootstrap 比例)
    try:
        import numpy as np
        arr = np.array(deltas)
        # 单样本 t-test 近似
        if len(deltas) > 1:
            mean = arr.mean()
            std = arr.std(ddof=1)
            if std > 0:
                t_stat = mean / (std / (len(deltas) ** 0.5))
                # 简化 p-value: 用正态近似
                from math import erf, sqrt
                p_value = 2 * (1 - 0.5 * (1 + erf(abs(t_stat) / sqrt(2))))
            else:
                p_value = 0.0 if mean != 0 else 1.0
        else:
            p_value = 1.0
    except (ImportError, Exception):
        p_value = 0.05 if is_significant else 1.0

    return {
        "delta_mean": delta_mean,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "is_significant": is_significant,
        "p_value": p_value,
    }


class ABTestRunner:
    """A/B 测试运行器 (shadow → canary → ramp).

    模式说明 (A/B Testing LLM Prompts 2026):
      - shadow: 0% 流量, 离线全量评估 30 case (主指标: 场景识别准确率)
      - canary: 离线全量 + LLM-as-Judge (10 个 golden case, 质量指标)
      - ramp: 同 shadow, 但需要 shadow + canary 都通过

    Args:
        mode: shadow / canary / ramp
        router: 可选 ModelRouter 实例. 注入后 canary 阶段真实调用 LLM 评分;
                不注入时 canary 返回占位结果 (向后兼容, CI 无 router 也能跑).
        judge_task_type: LLM-as-Judge 调用的 task_type (默认 chat_flash 控制成本)
        response_task_type: 候选矩阵生成回复的 task_type (默认 chat)
    """

    def __init__(
        self,
        mode: str = "shadow",
        router: Any = None,
        judge_task_type: str = "chat_flash",
        response_task_type: str = "chat",
    ) -> None:
        self.mode = mode
        self.router = router
        self.judge_task_type = judge_task_type
        self.response_task_type = response_task_type
        self.dataset = get_golden_dataset()

    def run_shadow(self, candidate_matrix: dict | None = None) -> ABTestReport:
        """Shadow 模式: 离线全量评估 (0 LLM 调用, 纯本地).

        主指标: 场景识别准确率 (30 case)
        副指标: 优先级匹配率, 缓存命中率
        统计: matched pairs + bootstrap CI
        """
        if candidate_matrix is None:
            # 从 prompt_complexity 获取候选矩阵
            from memory.prompt_complexity import (
                analyze_scene_complexity_alignment,
                recommend_priority_adjustment,
            )
            project_root = Path(__file__).parent.parent
            alignments = analyze_scene_complexity_alignment(project_root)
            candidate_matrix = recommend_priority_adjustment(alignments)

        control_matrix = _get_current_matrix()
        report = ABTestReport(
            timestamp=time.time(),
            mode=self.mode,
            n_cases=len(self.dataset),
        )

        # 在两个矩阵下分别跑所有 case (matched pairs)
        scores_a = []  # 场景识别正确性 (1.0 / 0.0)
        scores_b = []
        priority_a = []  # 优先级匹配 (1.0 / 0.0)
        priority_b = []
        cache_a = []  # 缓存命中 (1.0 / 0.0)
        cache_b = []

        for case in self.dataset:
            # A 组 (control)
            res_a = _run_single_case(case, control_matrix)
            # B 组 (candidate)
            res_b = _run_single_case(case, candidate_matrix)

            sa = 1.0 if res_a["scene_correct"] else 0.0
            sb = 1.0 if res_b["scene_correct"] else 0.0
            scores_a.append(sa)
            scores_b.append(sb)

            pa = 1.0 if res_a["priority_tail"] == case.expected_priority_tail else 0.0
            pb = 1.0 if res_b["priority_tail"] == case.expected_priority_tail else 0.0
            priority_a.append(pa)
            priority_b.append(pb)

            ca = 1.0 if res_a["cache_hit"] else 0.0
            cb = 1.0 if res_b["cache_hit"] else 0.0
            cache_a.append(ca)
            cache_b.append(cb)

            report.results.append(ABTestResult(
                case_id=case.case_id,
                scene=case.scene,
                a_scene_correct=res_a["scene_correct"],
                a_priority_tail=res_a["priority_tail"],
                a_cache_hit=res_a["cache_hit"],
                b_scene_correct=res_b["scene_correct"],
                b_priority_tail=res_b["priority_tail"],
                b_cache_hit=res_b["cache_hit"],
                scene_delta=sb - sa,
                priority_match_delta=pb - pa,
            ))

        # 计算指标
        n = len(self.dataset)
        report.a_accuracy = sum(scores_a) / n if n else 0
        report.b_accuracy = sum(scores_b) / n if n else 0
        report.accuracy_delta = report.b_accuracy - report.a_accuracy
        report.a_priority_match = sum(priority_a) / n if n else 0
        report.b_priority_match = sum(priority_b) / n if n else 0
        report.a_cache_hit_rate = sum(cache_a) / n if n else 0
        report.b_cache_hit_rate = sum(cache_b) / n if n else 0

        # 统计检验 (matched pairs on accuracy delta)
        stats = _matched_pair_test(scores_a, scores_b)
        report.bootstrap_ci_low = stats["ci_low"]
        report.bootstrap_ci_high = stats["ci_high"]
        report.is_significant = stats["is_significant"]
        report.p_value = stats["p_value"]

        # 决策 (shadow 阶段: 只看场景识别准确率)
        if report.accuracy_delta > 0 and report.is_significant:
            report.recommendation = "ship"  # B 显著优于 A, 建议上线
        elif report.accuracy_delta < 0 and report.is_significant:
            report.recommendation = "rollback"  # B 显著差于 A, 回滚
        elif abs(report.accuracy_delta) < 0.01:
            report.recommendation = "ship"  # 无差异, 采纳优化 (复杂度对齐更好)
        else:
            report.recommendation = "inconclusive"  # 不显著, 进入 canary

        logger.info("matrix_governance.ab_test_shadow",
                    mode=self.mode,
                    n=n,
                    a_acc=round(report.a_accuracy, 3),
                    b_acc=round(report.b_accuracy, 3),
                    delta=round(report.accuracy_delta, 3),
                    ci=f"[{report.bootstrap_ci_low:.3f}, {report.bootstrap_ci_high:.3f}]",
                    recommendation=report.recommendation)

        return report

    def run_canary(self, candidate_matrix: dict | None = None) -> ABTestReport:
        """Canary 模式: shadow 全量 + LLM-as-Judge (10 个 golden case).

        主指标: 场景识别准确率 + LLM-as-Judge 评分
        统计: matched pairs + bootstrap CI
        """
        # 先跑 shadow 全量
        report = self.run_shadow(candidate_matrix)

        # LLM-as-Judge (仅 10 个 case, 控制成本)
        try:
            judge_scores = self._run_llm_judge_subset(candidate_matrix)
            # 填充 LLM-as-Judge 字段到 report
            report.llm_judge_avg_score = judge_scores.get("avg_score", 0.0)
            report.llm_judge_n_cases = judge_scores.get("judged_cases", 0)
            report.llm_judge_note = judge_scores.get("note", "")
            # 合并决策
            report.recommendation = self._decide_with_judge(report, judge_scores)
        except Exception as e:
            logger.warning("matrix_governance.llm_judge_failed",
                           error=str(e),
                           fallback="使用 shadow 决策")

        return report

    def _run_llm_judge_subset(self, candidate_matrix: dict) -> dict:
        """对 10 个 golden case 运行 LLM-as-Judge (质量指标).

        成本控制 (GEM 2026):
          - 只跑 10 个 case (1/3), 每个场景第 1 个 (easy 难度, 控制变量)
          - 每个 case 2 次 LLM 调用: 1 次生成回复 + 1 次评分
          - 总计 20 次调用 (canary 阶段)

        Args:
            candidate_matrix: 候选矩阵 (用于生成回复)

        Returns:
            {
                "judged_cases": int,
                "scores": list[float],  # 1.0-5.0 每个案例的评分
                "avg_score": float,     # 平均评分 (1-5)
                "responses": list[str], # 候选矩阵生成的回复 (debug 用)
                "note": str,
            }
        """
        # 取每个场景的第 1 个 case (easy 难度, 控制变量)
        judged_cases = []
        seen_scenes = set()
        for case in self.dataset:
            if case.scene not in seen_scenes:
                judged_cases.append(case)
                seen_scenes.add(case.scene)
            if len(judged_cases) >= 10:
                break

        # 无 router 时返回占位 (向后兼容, CI 无 router 也能跑)
        if self.router is None:
            return {
                "judged_cases": len(judged_cases),
                "scores": [],
                "avg_score": 0.0,
                "responses": [],
                "note": "LLM-as-Judge 需注入 router, 当前返回占位",
            }

        # 真实调用 LLM 评分
        import asyncio
        import threading
        import prompt_builder
        scores: list[float] = []
        responses: list[str] = []

        def _run_coro_sync(coro):
            """在同步上下文中运行协程, 兼容已有 event loop 的情况.

            pytest-asyncio / FastAPI 等场景已有运行中的 loop,
            asyncio.run() 会报 RuntimeError. 用线程隔离的新 loop 解决.
            """
            try:
                asyncio.get_running_loop()
                in_loop = True
            except RuntimeError:
                in_loop = False

            if not in_loop:
                return asyncio.run(coro)

            # 已在 loop 中, 用线程隔离的新 loop
            result_holder: list = []
            def _run_in_new_loop():
                new_loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(new_loop)
                    result_holder.append(new_loop.run_until_complete(coro))
                finally:
                    new_loop.close()
            t = threading.Thread(target=_run_in_new_loop)
            t.start()
            t.join()
            return result_holder[0] if result_holder else None

        # 临时切换矩阵 (生成回复时用候选矩阵)
        original_matrix = copy.deepcopy(prompt_builder._MODULE_SCENE_PRIORITY)
        try:
            prompt_builder._MODULE_SCENE_PRIORITY = candidate_matrix
            for case in judged_cases:
                try:
                    # Step 1: 用候选矩阵构建 prompt 并生成回复
                    system_prompt = prompt_builder.build_scene_aware_prompt(
                        case.input, "评测员"
                    )
                    response = _run_coro_sync(self.router.route(
                        task_type=self.response_task_type,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": case.input},
                        ],
                        temperature=0.7,
                        max_tokens=500,
                        timeout=30,
                    ))
                    response = str(response) if response else ""

                    # Step 2: 用 LLM_JUDGE_RUBRIC 评分
                    judge_prompt = LLM_JUDGE_RUBRIC.format(
                        user_input=case.input,
                        reference_answer=case.reference_answer or "(无参考答案)",
                        response=response,
                    )
                    judge_response = _run_coro_sync(self.router.route(
                        task_type=self.judge_task_type,
                        messages=[{"role": "user", "content": judge_prompt}],
                        temperature=0.0,  # GEM 2026: 评分用 greedy (这里用 0 模拟)
                        max_tokens=100,
                        timeout=20,
                    ))
                    score = self._parse_judge_score(str(judge_response))
                    scores.append(score)
                    responses.append(response)
                    logger.info(
                        "matrix_governance.llm_judge_case",
                        case_id=case.case_id,
                        score=score,
                        response_len=len(response),
                    )
                except Exception as e:
                    logger.warning(
                        "matrix_governance.llm_judge_case_failed",
                        case_id=case.case_id,
                        error=str(e),
                    )
                    scores.append(0.0)
                    responses.append("")
        finally:
            prompt_builder._MODULE_SCENE_PRIORITY = original_matrix

        avg_score = sum(scores) / len(scores) if scores else 0.0
        return {
            "judged_cases": len(judged_cases),
            "scores": scores,
            "avg_score": avg_score,
            "responses": responses,
            "note": f"LLM-as-Judge 完成, 平均评分 {avg_score:.2f}/5.00",
        }

    @staticmethod
    def _parse_judge_score(text: str) -> float:
        """解析 LLM-as-Judge 返回的评分 JSON.

        GEM 2026: 期望格式 {"score": <1-5>, "reason": "..."}
        容错: 提取首个 1-5 整数作为评分.
        """
        import re
        # 尝试 JSON 解析
        try:
            # 找到第一个 {...} 块
            match = re.search(r'\{[^}]*"score"[^}]*\}', text, re.DOTALL)
            if match:
                obj = json.loads(match.group(0))
                score = float(obj.get("score", 0))
                if 1.0 <= score <= 5.0:
                    return score
        except (json.JSONDecodeError, ValueError):
            pass
        # 容错: 找第一个 1-5 的整数
        m = re.search(r'\b([1-5])\b', text)
        return float(m.group(1)) if m else 0.0

    def _decide_with_judge(self, report: ABTestReport, judge: dict) -> str:
        """结合 shadow 指标和 LLM-as-Judge 评分做决策.

        决策矩阵 (GEM 2026 + FutureAGI 2026):
          - shadow 显著 (CI 不跨 0): 直接用 shadow 决策 (主指标已够强)
          - shadow 不显著 + LLM 评分 >= 4.0: ship (质量好)
          - shadow 不显著 + LLM 评分 <= 2.0: rollback (质量差)
          - shadow 不显著 + LLM 评分 (2.0, 4.0): inconclusive
          - 无 router (占位): 退回 shadow 决策
        """
        if report.is_significant:
            return report.recommendation

        # 无 router 时退回 shadow 决策
        if not judge.get("scores"):
            return report.recommendation

        avg = judge.get("avg_score", 0.0)
        if avg >= 4.0:
            return "ship"
        if avg <= 2.0:
            return "rollback"
        return "inconclusive"

    def run_full_eval(self, candidate_matrix: dict | None = None) -> ABTestReport:
        """完整评估流程 (shadow → canary 判断).

        自动决定模式:
          - shadow 通过 (ship/inconclusive) → 返回 shadow 报告
          - shadow 不通过 (rollback) → 直接返回, 不进 canary
        """
        shadow_report = self.run_shadow(candidate_matrix)

        if shadow_report.recommendation == "rollback":
            return shadow_report  # shadow 已退化, 不进 canary

        # shadow 通过, 进入 canary (含 LLM-as-Judge)
        if self.mode == "canary":
            return self.run_canary(candidate_matrix)

        return shadow_report


# ============================================================================
# L5: 效果验证 — 4 量化指标 + 自动回滚阈值
# ============================================================================

@dataclass
class MatrixHealthReport:
    """矩阵健康报告 (4 量化指标)."""
    timestamp: float
    # 主指标 1: 场景识别准确率 (Golden Dataset)
    scene_accuracy: float = 0.0
    # 主指标 2: 复杂度对齐率 (is_aligned 场景数 / 总场景数)
    complexity_alignment: float = 0.0
    # 副指标 1: Prefix 缓存命中率
    cache_hit_rate: float = 0.0
    # 副指标 2: 优先级匹配率 (矩阵功能性)
    priority_match_rate: float = 0.0
    # 决策
    should_rollback: bool = False
    rollback_reasons: list[str] = field(default_factory=list)
    # 详情
    n_cases: int = 0
    n_aligned_scenes: int = 0
    n_total_scenes: int = 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "scene_accuracy": round(self.scene_accuracy, 4),
            "complexity_alignment": round(self.complexity_alignment, 4),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "priority_match_rate": round(self.priority_match_rate, 4),
            "should_rollback": self.should_rollback,
            "rollback_reasons": self.rollback_reasons,
            "n_cases": self.n_cases,
            "n_aligned_scenes": self.n_aligned_scenes,
            "n_total_scenes": self.n_total_scenes,
        }


# 回滚阈值 (用户确认: 完全自动模式)
ROLLBACK_THRESHOLDS = {
    "scene_accuracy_drop": 0.03,  # 场景识别准确率下降 > 3% → 回滚
    "cache_hit_rate_drop": 0.05,  # 缓存命中率下降 > 5% → 回滚
    "priority_match_drop": 0.05,  # 优先级匹配率下降 > 5% → 回滚
    # complexity_alignment 不设回滚阈值 (优化可能短期降低对齐率)
}


def evaluate_matrix_health(baseline: dict | None = None) -> MatrixHealthReport:
    """评估当前矩阵健康度 (L5 效果验证).

    4 量化指标:
      1. scene_accuracy: 场景识别准确率 (Golden Dataset 30 case)
      2. complexity_alignment: 复杂度对齐率 (is_aligned 场景数 / 总场景数)
      3. cache_hit_rate: Prefix 缓存命中率 (从 prompt_builder 获取)
      4. priority_match_rate: 优先级匹配率 (矩阵功能性)

    自动回滚:
      - scene_accuracy 下降 > 3% → 回滚
      - cache_hit_rate 下降 > 5% → 回滚
      - priority_match_rate 下降 > 5% → 回滚

    Args:
        baseline: 基线指标 (优化前), 如不提供则用默认基线

    Returns:
        MatrixHealthReport
    """
    report = MatrixHealthReport(timestamp=time.time())

    # 指标 1: 场景识别准确率
    dataset = get_golden_dataset()
    report.n_cases = len(dataset)
    correct = 0
    priority_correct = 0
    current_matrix = _get_current_matrix()

    for case in dataset:
        res = _run_single_case(case, current_matrix)
        if res["scene_correct"]:
            correct += 1
        if res["priority_tail"] == case.expected_priority_tail:
            priority_correct += 1

    report.scene_accuracy = correct / report.n_cases if report.n_cases else 0
    report.priority_match_rate = priority_correct / report.n_cases if report.n_cases else 0

    # 指标 2: 复杂度对齐率
    try:
        from memory.prompt_complexity import analyze_scene_complexity_alignment
        project_root = Path(__file__).parent.parent
        alignments = analyze_scene_complexity_alignment(project_root)
        report.n_total_scenes = len(alignments)
        report.n_aligned_scenes = sum(1 for a in alignments if a.is_aligned)
        report.complexity_alignment = (
            report.n_aligned_scenes / report.n_total_scenes
            if report.n_total_scenes else 0
        )
    except Exception as e:
        logger.warning("matrix_governance.complexity_eval_failed", error=str(e))

    # 指标 3: 缓存命中率
    try:
        import prompt_builder
        stats = prompt_builder.get_scene_cache_stats()
        report.cache_hit_rate = stats.get("hit_rate", 0.0)
    except (ImportError, AttributeError):
        pass

    # 回滚判断 (对比 baseline)
    if baseline:
        scene_drop = baseline.get("scene_accuracy", report.scene_accuracy) - report.scene_accuracy
        cache_drop = baseline.get("cache_hit_rate", report.cache_hit_rate) - report.cache_hit_rate
        priority_drop = baseline.get("priority_match_rate", report.priority_match_rate) - report.priority_match_rate

        if scene_drop > ROLLBACK_THRESHOLDS["scene_accuracy_drop"]:
            report.should_rollback = True
            report.rollback_reasons.append(
                f"场景识别准确率下降 {scene_drop:.1%} > 阈值 {ROLLBACK_THRESHOLDS['scene_accuracy_drop']:.1%}"
            )
        if cache_drop > ROLLBACK_THRESHOLDS["cache_hit_rate_drop"]:
            report.should_rollback = True
            report.rollback_reasons.append(
                f"缓存命中率下降 {cache_drop:.1%} > 阈值 {ROLLBACK_THRESHOLDS['cache_hit_rate_drop']:.1%}"
            )
        if priority_drop > ROLLBACK_THRESHOLDS["priority_match_drop"]:
            report.should_rollback = True
            report.rollback_reasons.append(
                f"优先级匹配率下降 {priority_drop:.1%} > 阈值 {ROLLBACK_THRESHOLDS['priority_match_drop']:.1%}"
            )

    logger.info("matrix_governance.health_evaluated",
                scene_acc=round(report.scene_accuracy, 3),
                alignment=round(report.complexity_alignment, 3),
                cache=round(report.cache_hit_rate, 3),
                priority=round(report.priority_match_rate, 3),
                should_rollback=report.should_rollback)

    return report


def rollback_if_degraded(baseline: dict) -> bool:
    """如果矩阵退化则自动回滚 (L5 反馈调整).

    用法:
      # 优化前记录基线
      baseline = evaluate_matrix_health().to_dict()
      # 应用优化
      auto_optimize_matrix(dry_run=False)
      # 验证, 退化则自动回滚
      rollback_if_degraded(baseline)

    Returns:
        True 表示已回滚, False 表示未退化
    """
    health = evaluate_matrix_health(baseline)
    if health.should_rollback:
        logger.warning("matrix_governance.auto_rollback",
                       reasons=health.rollback_reasons)
        return rollback_snapshot()
    return False


def capture_baseline() -> dict:
    """捕获当前矩阵的基线指标 (用于优化前后对比)."""
    return evaluate_matrix_health().to_dict()


# ============================================================================
# 完整闭环入口: optimize_and_validate
# ============================================================================

def optimize_and_validate(
    auto_apply: bool = False,
    full_eval: bool = True,
) -> dict:
    """完整闭环: 检测 → 优化 → A/B 测试 → 效果验证 → 反馈调整.

    流程:
      1. 捕获基线 (L5)
      2. dry-run 看推荐 diff (L3)
      3. 应用优化 (L3, 需要环境变量门控)
      4. shadow A/B 测试 (L4)
      5. 效果验证 + 自动回滚 (L5)

    Args:
        auto_apply: 是否自动应用 (需要 PROMPT_MATRIX_AUTO_APPLY=1)
        full_eval: 是否跑完整 A/B 评估 (False 只 dry-run)

    Returns:
        {
            "baseline": {...},
            "diff_count": int,
            "applied": bool,
            "ab_test": {...} | None,
            "health": {...},
            "rolled_back": bool,
        }
    """
    result = {
        "baseline": None,
        "diff_count": 0,
        "applied": False,
        "ab_test": None,
        "health": None,
        "rolled_back": False,
    }

    # 1. 捕获基线
    result["baseline"] = capture_baseline()

    # 2. dry-run
    dry_run_result = auto_optimize_matrix(dry_run=True)
    result["diff_count"] = dry_run_result["diff_count"]

    if not auto_apply or result["diff_count"] == 0:
        return result

    # 3. 应用优化 (环境变量门控)
    apply_result = auto_optimize_matrix(dry_run=False)
    result["applied"] = apply_result["applied"]
    if not result["applied"]:
        return result

    if not full_eval:
        return result

    # 4. shadow A/B 测试 (轻量模式跳过 — 纯本地 0 LLM 调用不跑 A/B)
    if GOVERNANCE_LIGHTWEIGHT:
        logger.info("matrix_governance轻量模式跳过L4_A/B测试")
        result["ab_test"] = {"recommendation": "skip_lightweight"}
    else:
        runner = ABTestRunner(mode="shadow")
        ab_report = runner.run_full_eval()
        result["ab_test"] = ab_report.to_dict()

    # 5. 效果验证 + 自动回滚
    health = evaluate_matrix_health(result["baseline"])
    result["health"] = health.to_dict()

    ab_rec = result["ab_test"].get("recommendation", "accept")
    if health.should_rollback or ab_rec == "rollback":
        result["rolled_back"] = rollback_snapshot()
        logger.info("matrix_governance闭环完成_已回滚",
                    reason="health_degraded_or_ab_rollback")
    else:
        logger.info("matrix_governance闭环完成_已采纳",
                    ab_recommendation=ab_rec)

    return result
