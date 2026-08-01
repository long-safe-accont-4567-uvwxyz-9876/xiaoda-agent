"""元认知引擎 (合并版) — 状态追踪 + 5 阶段反幻觉

合并自 core/meta_cognition.py 和 core/metacognition_lite.py。

5 阶段:
1. Anticipate  (预判): 识别任务相关信息 + 缺失信息
2. Plan        (规划): 制定推理路径
3. Monitor     (监控): 推理过程中检测幻觉/漂移
4. Reflect     (反思): 评估推理质量
5. Regulate    (调控): 调整策略 / 触发纠错
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger


# ============================================================
# 漂移类型
# ============================================================

class DriftType(str, Enum):
    """元认知漂移类型"""
    NONE = "none"
    HALLUCINATION = "hallucination"
    TOPIC_DRIFT = "topic_drift"
    REPETITION = "repetition"
    OVER_CONFIDENCE = "over_confidence"
    LOW_CONFIDENCE = "low_confidence"


# ============================================================
# Agent 自我状态 (来自原 meta_cognition.py)
# ============================================================

@dataclass
class AgentSelfState:
    """Agent 自我状态"""
    confidence: float = 1.0
    fatigue: float = 0.0
    error_rate: float = 0.0
    memory_pressure: float = 0.0
    total_turns: int = 0
    avg_response_ms: float = 0.0

    @property
    def health_score(self) -> float:
        return max(0, min(1, (
            self.confidence * 0.3
            + (1 - self.fatigue) * 0.2
            + (1 - self.error_rate) * 0.15
            + (1 - self.memory_pressure) * 0.15
            + 0.2
        )))

    @property
    def self_diagnosis(self) -> str:
        parts = []
        if self.confidence < 0.5:
            parts.append(f"信心不足({self.confidence:.2f})")
        if self.fatigue > 0.6:
            parts.append(f"疲劳度高({self.fatigue:.2f})")
        if self.error_rate > 0.2:
            parts.append(f"错误率偏高({self.error_rate:.2f})")
        if self.memory_pressure > 0.7:
            parts.append(f"内存压力大({self.memory_pressure:.2f})")
        return "; ".join(parts) if parts else "状态良好"


# ============================================================
# 元认知状态 (来自原 metacognition_lite.py)
# ============================================================

@dataclass
class MetacogState:
    """元认知状态"""
    known_facts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    task_keywords: list[str] = field(default_factory=list)
    plan_steps: list[str] = field(default_factory=list)
    confidence: float = 0.5
    uncertainty: float = 0.5
    drift_type: DriftType = DriftType.NONE
    drift_score: float = 0.0
    repetition_count: int = 0
    reflection: str = ""
    quality_score: float = 0.0
    action: str = "continue"
    target_step: int | None = None
    started_at: float = field(default_factory=time.time)
    history: deque = field(default_factory=lambda: deque(maxlen=100))


# ============================================================
# 元认知引擎: 状态追踪
# ============================================================

class MetaCognition:
    """元认知引擎 — 实时状态追踪与自省"""

    def __init__(self) -> None:
        self._state = AgentSelfState()
        self._error_history: deque = deque(maxlen=50)
        self._latency_history: deque = deque(maxlen=50)

    def record_success(self, latency_ms: float, confidence: float = 1.0) -> None:
        self._latency_history.append(latency_ms)
        self._error_history.append(1)
        self._state.total_turns += 1
        self._state.avg_response_ms = sum(self._latency_history) / len(self._latency_history)
        self._state.confidence = confidence
        self._state.error_rate = 1 - (sum(self._error_history) / max(1, len(self._error_history)))
        self._state.fatigue = min(1.0, self._state.total_turns / 200)

    def record_failure(self, latency_ms: float) -> None:
        self._error_history.append(0)
        self._latency_history.append(latency_ms)
        self._state.total_turns += 1
        self._state.error_rate = 1 - (sum(self._error_history) / max(1, len(self._error_history)))
        self._state.confidence = max(0, self._state.confidence - 0.1)

    def set_memory_pressure(self, used: float, total: float) -> None:
        self._state.memory_pressure = used / total if total > 0 else 0

    def get_status_report(self) -> dict:
        return {
            "health_score": round(self._state.health_score, 3),
            "diagnosis": self._state.self_diagnosis,
            "confidence": round(self._state.confidence, 3),
            "fatigue": round(self._state.fatigue, 3),
            "error_rate": round(self._state.error_rate, 3),
            "memory_pressure": round(self._state.memory_pressure, 3),
            "total_turns": self._state.total_turns,
            "avg_response_ms": round(self._state.avg_response_ms, 1),
        }


# ============================================================
# 元认知引擎: 5 阶段反幻觉
# ============================================================

class MetacognitionLite:
    """5 阶段元认知引擎 (轻量级, 纯推理时)"""

    def __init__(self) -> None:
        self.state = MetacogState()

    def anticipate(self, task: str, known: list[str] | None = None,
                    unknown: list[str] | None = None) -> MetacogState:
        keywords = re.findall(r'\b[a-zA-Z_]{3,}\b', task.lower())
        self.state.task_keywords = list(set(keywords))
        self.state.known_facts = list(known or [])
        self.state.unknowns = list(unknown or [])
        if self.state.unknowns:
            self.state.uncertainty = min(1.0, 0.3 + 0.15 * len(self.state.unknowns))
        return self.state

    def plan(self, steps: list[str]) -> MetacogState:
        self.state.plan_steps = list(steps)
        return self.state

    def monitor(self, step_output: str, confidence: float = 0.5,
                 step_idx: int | None = None) -> DriftType:
        self.state.confidence = max(0.0, min(1.0, confidence))
        last_entries = [h.get("output", "") for h in list(self.state.history)[-3:]]
        if step_output in last_entries:
            self.state.repetition_count += 1
        else:
            self.state.repetition_count = max(0, self.state.repetition_count - 1)
        if self.state.task_keywords:
            kw_in_output = sum(1 for k in self.state.task_keywords if k in step_output.lower())
            relevance = kw_in_output / len(self.state.task_keywords)
            self.state.drift_score = max(0.0, 1.0 - relevance)
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
        self.state.history.append({
            "step": step_idx, "output": step_output[:200],
            "confidence": confidence, "drift": self.state.drift_type.value,
        })
        return self.state.drift_type

    def reflect(self, final_answer: str) -> dict:
        if self.state.task_keywords:
            covered = sum(1 for k in self.state.task_keywords if k in final_answer.lower())
            coverage = covered / len(self.state.task_keywords)
        else:
            coverage = 1.0
        self.state.quality_score = (
            0.4 * self.state.confidence + 0.3 * coverage +
            0.2 * (1 - self.state.drift_score) + 0.1 * (1 - min(1.0, self.state.uncertainty))
        )
        if self.state.drift_type != DriftType.NONE:
            self.state.reflection = f"Drift: {self.state.drift_type.value}. Coverage={coverage:.2f}."
        elif self.state.quality_score > 0.7:
            self.state.reflection = "High quality answer, no drift."
        else:
            self.state.reflection = f"Quality below threshold ({self.state.quality_score:.2f})."
        return {
            "quality_score": self.state.quality_score,
            "confidence": self.state.confidence,
            "uncertainty": self.state.uncertainty,
            "drift_type": self.state.drift_type.value,
            "drift_score": self.state.drift_score,
            "coverage": coverage,
            "reflection": self.state.reflection,
        }

    def regulate(self) -> str:
        if self.state.drift_type == DriftType.REPETITION:
            self.state.action = "reframe"
        elif self.state.drift_type == DriftType.TOPIC_DRIFT:
            self.state.action = "retry"
        elif self.state.drift_type == DriftType.OVER_CONFIDENCE:
            self.state.action = "verify"
        elif self.state.drift_type == DriftType.LOW_CONFIDENCE:
            self.state.action = "request_more_info"
        else:
            self.state.action = "continue"
        return self.state.action

    def get_state_dict(self) -> dict:
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


# ============================================================
# 全局单例
# ============================================================

_meta_cognition = MetaCognition()


def get_meta_cognition() -> MetaCognition:
    """获取全局 MetaCognition 单例."""
    return _meta_cognition
