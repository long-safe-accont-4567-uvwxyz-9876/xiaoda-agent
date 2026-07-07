"""Metacognition-Lite (A2) — 5 阶段反幻觉+漂移检测

参考:
- MaR: Metacognition-as-Reward (ICLR 2026, OpenCausaLab)
- Meta AI: Metacognitive Reuse (Behavior Handbook)
- SelfAware / MetaMedQA benchmarks

5 阶段:
1. Anticipate  (预判): 识别任务相关信息 + 缺失信息
2. Plan        (规划): 制定推理路径
3. Monitor     (监控): 推理过程中检测幻觉/漂移
4. Reflect     (反思): 评估推理质量
5. Regulate    (调控): 调整策略 / 触发纠错

特性:
- 不依赖训练, 纯推理时元认知
- 输出 confidence + uncertainty + drift_score
- 触发条件: 低置信度 / 高不确定性 / 漂移检测
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loguru import logger


class DriftType(str, Enum):
    """元认知漂移类型枚举，标识幻觉、主题漂移、重复等异常状态。"""
    NONE = "none"
    HALLUCINATION = "hallucination"      # 幻觉: 输出与已知事实冲突
    TOPIC_DRIFT = "topic_drift"           # 主题漂移
    REPETITION = "repetition"             # 重复循环
    OVER_CONFIDENCE = "over_confidence"   # 过度自信
    LOW_CONFIDENCE = "low_confidence"     # 置信度过低


@dataclass
class MetacogState:
    """元认知状态"""
    # 阶段 1: Anticipate
    known_facts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    task_keywords: list[str] = field(default_factory=list)
    # 阶段 2: Plan
    plan_steps: list[str] = field(default_factory=list)
    # 阶段 3: Monitor
    confidence: float = 0.5
    uncertainty: float = 0.5
    drift_type: DriftType = DriftType.NONE
    drift_score: float = 0.0
    repetition_count: int = 0
    # 阶段 4: Reflect
    reflection: str = ""
    quality_score: float = 0.0
    # 阶段 5: Regulate
    action: str = "continue"              # continue / retry / reframe / abort
    target_step: Optional[int] = None
    # 时间戳
    started_at: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)


class MetacognitionLite:
    """5 阶段元认知引擎 (轻量级, 纯推理时)

    用法:
        mc = MetacognitionLite()
        mc.anticipate("what's the weather in tokyo", known=["location:tokyo"])
        mc.plan(["check weather", "format response"])
        # 推理循环
        for step in steps:
            mc.monitor(step_output, confidence=0.8)
            if mc.state.drift_type != DriftType.NONE:
                mc.regulate()
                if mc.state.action == "retry":
                    break
            else:
                mc.state.action = "continue"
        mc.reflect(final_answer)
    """

    def __init__(self) -> None:
        self.state = MetacogState()

    # ─── 阶段 1: Anticipate ───
    def anticipate(self, task: str, known: Optional[list[str]] = None,
                    unknown: Optional[list[str]] = None) -> MetacogState:
        """识别任务相关信息"""
        # 关键词抽取
        keywords = re.findall(r'\b[a-zA-Z_]{3,}\b', task.lower())
        self.state.task_keywords = list(set(keywords))
        self.state.known_facts = list(known or [])
        self.state.unknowns = list(unknown or [])
        # 不确定性估计
        if self.state.unknowns:
            self.state.uncertainty = min(1.0, 0.3 + 0.15 * len(self.state.unknowns))
        logger.debug(f"MC.anticipate keywords={len(self.state.task_keywords)} "
                      f"unknowns={len(self.state.unknowns)} "
                      f"uncertainty={self.state.uncertainty:.2f}")
        return self.state

    # ─── 阶段 2: Plan ───
    def plan(self, steps: list[str]) -> MetacogState:
        """规划推理步骤"""
        self.state.plan_steps = list(steps)
        logger.debug(f"MC.plan steps={len(steps)}")
        return self.state

    # ─── 阶段 3: Monitor ───
    def monitor(self, step_output: str, confidence: float = 0.5,
                 step_idx: Optional[int] = None) -> DriftType:
        """监控单步推理输出, 检测漂移"""
        self.state.confidence = max(0.0, min(1.0, confidence))

        # 检测重复
        last_entries = [h.get("output", "") for h in self.state.history[-3:]]
        if step_output in last_entries:
            self.state.repetition_count += 1
        else:
            self.state.repetition_count = max(0, self.state.repetition_count - 1)

        # 检测漂移: 输出与任务关键词的相关性
        if self.state.task_keywords:
            kw_in_output = sum(1 for k in self.state.task_keywords if k in step_output.lower())
            relevance = kw_in_output / len(self.state.task_keywords)
            self.state.drift_score = max(0.0, 1.0 - relevance)

        # 判定漂移类型
        if self.state.repetition_count >= 2:
            self.state.drift_type = DriftType.REPETITION
        elif self.state.drift_score > 0.7:
            self.state.drift_type = DriftType.TOPIC_DRIFT
        elif confidence > 0.95 and self.state.uncertainty > 0.5:
            self.state.drift_type = DriftType.OVER_CONFIDENCE
        elif confidence < 0.2:
            self.state.drift_type = DriftType.LOW_CONFIDENCE
        else:
            self.state.drift_type = DriftType.NONE

        # 记录历史
        self.state.history.append({
            "step": step_idx,
            "output": step_output[:200],
            "confidence": confidence,
            "drift": self.state.drift_type.value,
        })

        logger.debug(f"MC.monitor confidence={confidence:.2f} "
                      f"drift={self.state.drift_type.value} "
                      f"score={self.state.drift_score:.2f}")
        return self.state.drift_type

    # ─── 阶段 4: Reflect ───
    def reflect(self, final_answer: str) -> dict:
        """反思最终输出"""
        # 信息覆盖率
        if self.state.task_keywords:
            covered = sum(1 for k in self.state.task_keywords if k in final_answer.lower())
            coverage = covered / len(self.state.task_keywords)
        else:
            coverage = 1.0

        # 综合质量分
        self.state.quality_score = (
            0.4 * self.state.confidence +
            0.3 * coverage +
            0.2 * (1 - self.state.drift_score) +
            0.1 * (1 - min(1.0, self.state.uncertainty))
        )

        # 生成反思
        if self.state.drift_type != DriftType.NONE:
            self.state.reflection = (
                f"Drift detected: {self.state.drift_type.value}. "
                f"Coverage={coverage:.2f}, Uncertainty={self.state.uncertainty:.2f}. "
                f"Action recommended: {self._recommend_action()}."
            )
        elif self.state.quality_score > 0.7:
            self.state.reflection = "High quality answer, no drift."
        else:
            self.state.reflection = (
                f"Quality below threshold ({self.state.quality_score:.2f}). "
                f"Consider: re-planning steps, requesting more context."
            )

        return {
            "quality_score": self.state.quality_score,
            "confidence": self.state.confidence,
            "uncertainty": self.state.uncertainty,
            "drift_type": self.state.drift_type.value,
            "drift_score": self.state.drift_score,
            "coverage": coverage,
            "reflection": self.state.reflection,
        }

    # ─── 阶段 5: Regulate ───
    def regulate(self) -> str:
        """根据反思结果调整策略"""
        self.state.action = self._recommend_action()
        if self.state.drift_type == DriftType.REPETITION:
            self.state.action = "reframe"
        elif self.state.drift_type == DriftType.TOPIC_DRIFT:
            self.state.action = "retry"
            # 回溯到第一个漂移的步骤
            for i, h in enumerate(self.state.history):
                if h.get("drift") not in (None, "none"):
                    self.state.target_step = i
                    break
        elif self.state.drift_type == DriftType.OVER_CONFIDENCE:
            self.state.action = "verify"
        elif self.state.drift_type == DriftType.LOW_CONFIDENCE:
            self.state.action = "request_more_info"
        else:
            self.state.action = "continue"
        logger.info(f"MC.regulate action={self.state.action} "
                     f"target_step={self.state.target_step}")
        return self.state.action

    def _recommend_action(self) -> str:
        """推荐动作"""
        if self.state.quality_score < 0.3:
            return "abort"
        if self.state.drift_type == DriftType.REPETITION:
            return "reframe"
        if self.state.drift_type == DriftType.TOPIC_DRIFT:
            return "retry"
        if self.state.confidence < 0.3:
            return "request_more_info"
        return "continue"

    def get_state_dict(self) -> dict:
        """获取状态字典 (用于 /health/self 接口)"""
        return {
            "phase": "monitor" if self.state.history else "anticipate",
            "confidence": self.state.confidence,
            "uncertainty": self.state.uncertainty,
            "drift_type": self.state.drift_type.value,
            "drift_score": self.state.drift_score,
            "quality_score": self.state.quality_score,
            "action": self.state.action,
            "steps_total": len(self.state.plan_steps),
            "steps_executed": len(self.state.history),
            "known_facts": len(self.state.known_facts),
            "unknowns": len(self.state.unknowns),
        }
