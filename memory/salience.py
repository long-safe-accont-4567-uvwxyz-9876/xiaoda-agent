# memory/salience.py
"""情绪加权 Salience 评分器

mazemaker原版: 0.6×recency + 0.4×frequency
xiaoda扩展: 0.4×recency + 0.3×frequency + 0.3×emotion
"""
from __future__ import annotations

import math
import time
from typing import Any


class SalienceScorer:
    """情绪加权 Salience 评分器

    三维融合:
    - Recency: 指数衰减 (1小时半衰期)
    - Frequency: 对数归一化访问次数
    - Emotion: PAD arousal + 情绪标签匹配度
    """

    RECENCY_HALF_LIFE = 3600.0   # 1小时半衰期 (秒)
    FREQ_LOG_BASE = 10.0
    # 权重: mazemaker原版 0.6/0.4, xiaoda扩展为 0.4/0.3/0.3
    W_RECENCY = 0.4
    W_FREQUENCY = 0.3
    W_EMOTION = 0.3

    def compute(self, entry: Any, now: float | None = None,
                pad_state: Any | None = None) -> float:
        """计算综合 Salience 评分

        Args:
            entry: 记忆条目 (需有 access_count, last_accessed, emotion_label 属性)
            now: 当前时间戳, 默认 time.time()
            pad_state: PAD情绪状态 (需有 arousal, dominant_emotion 属性)

        Returns:
            salience 评分 [0, 1]
        """
        if now is None:
            now = time.time()

        recency_score = self._recency_score(entry, now)
        freq_score = self._frequency_score(entry)
        emotion_score = self._emotion_score(entry, pad_state)

        return (self.W_RECENCY * recency_score
                + self.W_FREQUENCY * freq_score
                + self.W_EMOTION * emotion_score)

    def _recency_score(self, entry: Any, now: float) -> float:
        """时近性评分: 从最后访问时间起指数衰减"""
        last_accessed = getattr(entry, 'last_accessed', 0) or getattr(entry, 'created_at', 0)
        if last_accessed == 0:
            return 0.0
        recency_seconds = max(0, now - last_accessed)
        return math.exp(-recency_seconds / self.RECENCY_HALF_LIFE)

    def _frequency_score(self, entry: Any) -> float:
        """频率评分: 对数归一化访问次数"""
        access_count = getattr(entry, 'access_count', 0)
        return min(math.log1p(access_count) / self.FREQ_LOG_BASE, 1.0)

    def _emotion_score(self, entry: Any, pad_state: Any | None) -> float:
        """情绪评分: 记忆emotion_label与当前PAD状态的匹配度

        - 无emotion_label → 返回 0.0 (无情绪维度贡献)
        - 有emotion_label但无PAD状态 → 返回中性 0.5
        - 有PAD: arousal强度×0.6 + 标签匹配×0.4

        Note: brief原版先检查pad_state再检查emotion_label, 导致无情绪标签的
        记忆仍得0.5中性分(30天旧记忆会得0.15而非≈0, 违反test_old_memory_low_score)。
        此处调整为先检查emotion_label, 与测试注释"recency_score ≈ 0"的意图一致。
        """
        emotion_label = getattr(entry, 'emotion_label', '')
        if not emotion_label:
            return 0.0

        if pad_state is None:
            return 0.5

        arousal = abs(getattr(pad_state, 'arousal', 0.0))
        dominant_emotion = getattr(pad_state, 'dominant_emotion', 'neutral')

        label_match = 1.0 if emotion_label == dominant_emotion else 0.3
        return min(1.0, arousal * 0.6 + label_match * 0.4)
