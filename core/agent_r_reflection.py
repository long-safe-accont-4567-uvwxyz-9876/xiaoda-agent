"""Agent-R 实时反思 (A3) — MCTS 风格的回溯修正

参考:
- Agent-R: Training Language Model Agents to Reflect via Iterative Self-Training
  (ByteDance Seed, arxiv 2501.11425)
- SAMULE: Self-Learning Agents Enhanced by Multi-level Reflection (EMNLP 2025)

核心思想:
1. 不等待整个轨迹结束, 中间步骤出错即可触发反思
2. 从失败步骤回溯, 与相邻正确路径拼接形成修正轨迹
3. 把反思转化为可复用的语言化记忆

特性:
- 在线反思: 检测到错误立即触发, 不必等到最终
- 轨迹拼接: 失败步骤 + 相邻正确路径 = 修正轨迹
- 反思记忆: 失败教训转为语言化约束, 下次自动注入
- 触发条件: 工具调用失败 / 输出与预期严重不符 / 元认知检测漂移

实现说明:
本实现是推理时版本 (非训练时), 不依赖 MCTS 模拟,
而是基于 Agent-R 的核心思想做轻量化落地:
- 失败步骤检测
- 上下文截断 + 重新规划
- 失败教训长期记忆
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loguru import logger


class TrajectoryType(str, Enum):
    """反思轨迹类型枚举，区分初始/错误/正确/修正四类轨迹。"""
    INITIAL = "initial"        # 初始轨迹
    ERROR = "error"            # 错误轨迹
    CORRECT = "correct"        # 正确轨迹
    REVISION = "revision"      # 修正轨迹 (错误+正确拼接)


@dataclass
class TrajectoryStep:
    """轨迹步骤"""
    step_idx: int
    action: str                  # tool_call / llm_response / observation
    content: str
    success: bool = True
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class Trajectory:
    """轨迹"""
    type: TrajectoryType
    steps: list[TrajectoryStep] = field(default_factory=list)
    reward: float = 0.0
    reflection: str = ""

    def find_first_error(self) -> Optional[int]:
        """找到第一个错误步骤的索引"""
        for i, s in enumerate(self.steps):
            if not s.success:
                return i
        return None

    def splice_with(self, correct: "Trajectory", from_step: int) -> "Trajectory":
        """与正确轨迹拼接, 形成修正轨迹

        保留 [0, from_step) + correct.steps[from_step:]
        """
        spliced = Trajectory(type=TrajectoryType.REVISION)
        spliced.steps = self.steps[:from_step] + correct.steps[from_step:]
        return spliced


@dataclass
class ReflectionMemory:
    """反思记忆 (长期)"""
    lesson: str                  # 教训描述
    pattern: str                 # 错误模式
    correction: str              # 修正策略
    occurrence_count: int = 1
    last_seen: float = field(default_factory=time.time)
    success_after_correction: int = 0


class AgentRReflector:
    """Agent-R 实时反思器

    用法:
        r = AgentRReflector()
        # 推理过程中
        r.record_step("tool_call", "...", success=False, error="404 not found")
        if r.should_reflect():
            memory = r.reflect()
            # 把 memory.lesson 注入到下次推理的 system prompt
    """

    def __init__(self, max_memory: int = 100) -> None:
        self._current_trajectory = Trajectory(type=TrajectoryType.INITIAL)
        self._past_trajectories: list[Trajectory] = []
        self._memories: list[ReflectionMemory] = []
        self._max_memory = max_memory
        self._revision_count = 0
        self._reflection_count = 0

    def record_step(self, action: str, content: str,
                     success: bool = True,
                     error: Optional[str] = None) -> None:
        """记录一步"""
        step = TrajectoryStep(
            step_idx=len(self._current_trajectory.steps),
            action=action, content=content,
            success=success, error=error,
        )
        self._current_trajectory.steps.append(step)
        # 实时检测: 第一个错误即触发
        if not success and self._current_trajectory.type == TrajectoryType.INITIAL:
            self._current_trajectory.type = TrajectoryType.ERROR
            logger.info(f"AgentR.error_detected step={step.step_idx} "
                         f"action={action} error={error}")

    def should_reflect(self) -> bool:
        """是否需要触发反思"""
        return self._current_trajectory.find_first_error() is not None

    def reflect(self) -> Optional[ReflectionMemory]:
        """触发反思, 生成记忆"""
        if not self.should_reflect():
            return None
        self._reflection_count += 1
        err_idx = self._current_trajectory.find_first_error()
        if err_idx is None:
            return None

        err_step = self._current_trajectory.steps[err_idx]
        # 提取错误模式
        pattern = self._extract_pattern(err_step)
        # 生成教训
        lesson = self._generate_lesson(err_step, pattern)
        # 修正策略
        correction = self._generate_correction(err_step, pattern)

        memory = ReflectionMemory(
            lesson=lesson,
            pattern=pattern,
            correction=correction,
        )

        # 检查是否已有相同模式的记忆
        existing = next((m for m in self._memories if m.pattern == pattern), None)
        if existing:
            existing.occurrence_count += 1
            existing.last_seen = time.time()
        else:
            self._memories.append(memory)
            # 超过上限时淘汰最旧的
            if len(self._memories) > self._max_memory:
                self._memories.sort(key=lambda m: m.last_seen)
                self._memories = self._memories[-self._max_memory:]

        self._current_trajectory.reflection = lesson
        logger.info(f"AgentR.reflect memory_added pattern={pattern} "
                     f"lesson={lesson[:80]}")

        # A4: 反思生成的教训同步记录到学习反馈闭环 (失败不阻塞)
        try:
            from core.learning_feedback import record_reflection_lesson
            record_reflection_lesson(
                lesson_text=lesson, pattern=pattern, correction=correction,
            )
        except Exception as e:
            logger.debug("agent_r_reflection.record_lesson_failed", exc_info=True)

        return memory

    def apply_revision(self, correct_steps: list[TrajectoryStep]) -> Trajectory:
        """应用修正: 用正确步骤替换错误步骤后的部分"""
        err_idx = self._current_trajectory.find_first_error()
        if err_idx is None:
            return self._current_trajectory

        correct_traj = Trajectory(type=TrajectoryType.CORRECT)
        correct_traj.steps = correct_steps

        revision = self._current_trajectory.splice_with(correct_traj, err_idx)
        revision.type = TrajectoryType.REVISION
        self._revision_count += 1

        # 记忆修正成功
        if self._memories:
            self._memories[-1].success_after_correction += 1

        # 归档当前轨迹
        self._past_trajectories.append(self._current_trajectory)
        self._current_trajectory = revision
        return revision

    def reset(self) -> None:
        """重置当前轨迹 (新一轮对话)"""
        if self._current_trajectory.steps:
            self._past_trajectories.append(self._current_trajectory)
        self._current_trajectory = Trajectory(type=TrajectoryType.INITIAL)

    def get_lessons_for_prompt(self, top_k: int = 3) -> str:
        """获取最近 top-k 教训, 注入到 system prompt"""
        if not self._memories:
            return ""
        sorted_m = sorted(self._memories,
                            key=lambda m: (m.occurrence_count, m.last_seen),
                            reverse=True)[:top_k]
        lines = ["Past lessons learned (apply to avoid repeating mistakes):"]
        for m in sorted_m:
            lines.append(f"- {m.lesson} → {m.correction}")
        return "\n".join(lines)

    def _extract_pattern(self, step: TrajectoryStep) -> str:
        """从错误步骤提取模式"""
        if step.error:
            # 简单模式提取: 取 error 前 50 字符
            err = step.error.lower()
            if "timeout" in err:
                return "timeout_error"
            if "not found" in err or "404" in err:
                return "resource_not_found"
            if "permission" in err or "403" in err:
                return "permission_denied"
            if "rate" in err and "limit" in err:
                return "rate_limited"
            if "auth" in err or "401" in err:
                return "auth_failed"
            return f"error_{step.action}"
        return f"bad_output_{step.action}"

    def _generate_lesson(self, step: TrajectoryStep, pattern: str) -> str:
        """生成教训"""
        lessons = {
            "timeout_error": f"When calling {step.action}, set adequate timeout. Last error: {step.error}",
            "resource_not_found": f"Verify resource exists before calling {step.action}.",
            "permission_denied": f"Check permissions before {step.action}.",
            "rate_limited": f"Add backoff/retry for {step.action}.",
            "auth_failed": f"Validate credentials before {step.action}.",
        }
        return lessons.get(pattern,
                              f"Avoid repeating: {step.action} failed with {step.error or 'bad output'}")

    def _generate_correction(self, step: TrajectoryStep, pattern: str) -> str:
        """生成修正策略"""
        corrections = {
            "timeout_error": "Increase timeout or split task into smaller calls",
            "resource_not_found": "Check existence with a probe call first",
            "permission_denied": "Request permission escalation or use fallback",
            "rate_limited": "Use exponential backoff: 1s, 2s, 4s, 8s",
            "auth_failed": "Refresh credentials and retry once",
        }
        return corrections.get(pattern, "Try alternative approach or skip")

    def get_stats(self) -> dict:
        """统计信息"""
        return {
            "current_trajectory_steps": len(self._current_trajectory.steps),
            "past_trajectories": len(self._past_trajectories),
            "memories": len(self._memories),
            "revision_count": self._revision_count,
            "reflection_count": self._reflection_count,
            "top_patterns": [m.pattern for m in
                              sorted(self._memories,
                                     key=lambda m: m.occurrence_count,
                                     reverse=True)[:5]],
        }
