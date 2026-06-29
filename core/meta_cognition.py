"""元认知: Agent 状态自省

Agent 自我状态感知 — confidence, fatigue, error_rate, memory_pressure
"""
from dataclasses import dataclass, field
from collections import deque
from loguru import logger


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
        """返回综合健康分 (0~1, 融合信心/疲劳/错误率/内存压力)."""
        return max(0, min(1, (
            self.confidence * 0.3
            + (1 - self.fatigue) * 0.2
            + (1 - self.error_rate) * 0.15
            + (1 - self.memory_pressure) * 0.15
            + 0.2
        )))

    @property
    def self_diagnosis(self) -> str:
        """返回状态自诊描述文本, 状态良好时返回 '状态良好'."""
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


class MetaCognition:
    """元认知引擎 — 实时状态追踪与自省"""

    def __init__(self) -> None:
        self._state = AgentSelfState()
        self._error_history: deque = deque(maxlen=50)
        self._latency_history: deque = deque(maxlen=50)

    def record_success(self, latency_ms: float, confidence: float = 1.0) -> None:
        """记录成功调用"""
        self._latency_history.append(latency_ms)
        self._state.total_turns += 1
        self._state.avg_response_ms = sum(self._latency_history) / len(self._latency_history)
        self._state.confidence = confidence
        self._state.error_rate = 1 - (sum(self._error_history) / max(1, len(self._error_history)))
        self._state.fatigue = min(1.0, self._state.total_turns / 200)

    def record_failure(self, latency_ms: float) -> None:
        """记录失败调用"""
        self._error_history.append(0)
        self._latency_history.append(latency_ms)
        self._state.total_turns += 1
        total = max(1, len(self._error_history))
        errors = total - sum(self._error_history)
        self._state.error_rate = errors / total
        self._state.confidence = max(0, self._state.confidence - 0.1)

    def set_memory_pressure(self, used: float, total: float) -> None:
        """设置内存压力"""
        self._state.memory_pressure = used / total if total > 0 else 0

    def get_status_report(self) -> dict:
        """获取状态报告"""
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


# 全局单例
_meta_cognition = MetaCognition()


def get_meta_cognition() -> MetaCognition:
    """获取全局 MetaCognition 单例."""
    return _meta_cognition
