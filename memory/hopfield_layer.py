# memory/hopfield_layer.py
"""Modern Hopfield Network (Transformer Attention)

核心算法: xi_new = sum_j softmax(beta * cos_sim(xi, xj)) * xj
beta=20 使注意力分布极尖锐 (近似one-hot)

源自 mazemaker src/memory/hopfield.cpp
论文: Ramsauer et al. (2020) "Hopfield Networks is All You Need"
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Pattern:
    """存储模式"""
    data: np.ndarray
    id: int = 0
    timestamp: float = field(default_factory=time.time)
    access_count: int = 0
    salience: float = 1.0
    label: str = ""
    source: str = "episodic"


@dataclass
class RetrievalResult:
    """检索结果"""
    pattern: np.ndarray = None
    confidence: float = 0.0
    pattern_id: int = 0
    entropy: float = 0.0
    converged: bool = False
    iterations: int = 0


class HopfieldLayer:
    """Modern Hopfield 联想记忆层

    使用迭代注意力实现模式完成:
    1. 初始化 current = cue
    2. scores = beta * cosine_sim(patterns, current)
    3. weights = softmax(scores) (数值稳定)
    4. next = weights @ patterns
    5. 收敛检查: ||next - current|| < eps
    """

    def __init__(self, dimensions: int = 512, capacity: int = 1024,
                 beta: float = 20.0, max_iterations: int = 10,
                 convergence_eps: float = 1e-4, decay_rate: float = 0.999) -> None:
        self.dimensions = dimensions
        self.capacity = capacity
        self.beta = beta
        self.max_iterations = max_iterations
        self.convergence_eps = convergence_eps
        self.decay_rate = decay_rate

        self._patterns: list[Pattern] = []
        self._patterns_lock = threading.Lock()
        self._next_id = 1

    def store(self, pattern: np.ndarray, label: str = "",
              source: str = "episodic") -> int:
        """存储模式, 满时驱逐最低salience"""
        if pattern.shape != (self.dimensions,):
            raise ValueError(f"Pattern dimension mismatch: expected {self.dimensions}, got {pattern.shape}")
        pattern = pattern.astype(np.float32)

        with self._patterns_lock:
            # 满时驱逐
            while len(self._patterns) >= self.capacity:
                self._evict_internal()

            p = Pattern(
                data=pattern.astype(np.float32).copy(),
                id=self._next_id,
                timestamp=time.time(),
                salience=1.0,
                label=label,
                source=source,
            )
            self._patterns.append(p)
            self._next_id += 1
            return p.id

    def retrieve(self, cue: np.ndarray) -> RetrievalResult:
        """迭代注意力检索"""
        cue = cue.astype(np.float32)
        result = RetrievalResult()

        with self._patterns_lock:
            if not self._patterns:
                result.pattern = cue.copy()
                return result

            current = cue.copy()
            if current.shape != (self.dimensions,):
                raise ValueError(f"Cue dimension mismatch: expected {self.dimensions}, got {current.shape}")

            for iteration in range(self.max_iterations):
                nxt = self._attention_sum(current)
                diff = float(np.linalg.norm(nxt - current))
                current = nxt
                result.iterations = iteration + 1

                if diff < self.convergence_eps:
                    result.converged = True
                    break

            result.pattern = current

            # 找最近存储模式计算 confidence
            best_sim = -1.0
            best_id = 0
            for p in self._patterns:
                sim = self._cosine_sim(current, p.data)
                if sim > best_sim:
                    best_sim = sim
                    best_id = p.id
            result.confidence = max(0.0, best_sim)
            result.pattern_id = best_id

            # 计算注意力分布的熵
            weights = self._attention_weights(current)
            if weights is not None:
                mask = weights > 1e-10
                if mask.any():
                    result.entropy = float(-np.sum(weights[mask] * np.log(weights[mask])))

            # 更新访问计数
            for p in self._patterns:
                if p.id == best_id:
                    p.access_count += 1
                    break

        return result

    def lookup(self, query: np.ndarray) -> RetrievalResult:
        """单次迭代检索"""
        return self.retrieve(query)

    def update_salience(self) -> None:
        """salience衰减 + recency/freq boost"""
        now = time.time()
        with self._patterns_lock:
            for p in self._patterns:
                p.salience *= self.decay_rate
                age_seconds = now - p.timestamp
                recency_boost = float(np.exp(-age_seconds / 3600.0))
                freq_boost = float(np.log1p(p.access_count) * 0.1)
                p.salience = min(1.0, max(p.salience, recency_boost + freq_boost))

    def pattern_count(self) -> int:
        with self._patterns_lock:
            return len(self._patterns)

    def pattern_ids(self) -> list[int]:
        with self._patterns_lock:
            return [p.id for p in self._patterns]

    def top_k(self, query: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
        """找K个最相似模式"""
        query = query.astype(np.float32)
        with self._patterns_lock:
            scored = [(p.id, self._cosine_sim(query, p.data)) for p in self._patterns]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def _attention_weights(self, query: np.ndarray) -> np.ndarray | None:
        """计算softmax注意力权重"""
        n = len(self._patterns)
        if n == 0:
            return None

        scores = np.empty(n, dtype=np.float32)
        for i, p in enumerate(self._patterns):
            scores[i] = self._cosine_sim(query, p.data) * self.beta

        # 数值稳定softmax: 减去max
        max_score = scores.max()
        weights = np.exp(scores - max_score)
        weights /= (weights.sum() + 1e-10)
        return weights

    def _attention_sum(self, query: np.ndarray) -> np.ndarray:
        """注意力加权和: output = sum_j weights[j] * pattern_j"""
        weights = self._attention_weights(query)
        if weights is None:
            return query.copy()

        result = np.zeros(self.dimensions, dtype=np.float32)
        for j, p in enumerate(self._patterns):
            if weights[j] > 1e-8:
                result += weights[j] * p.data
        return result

    def _evict_internal(self) -> None:
        """驱逐最低salience模式"""
        if not self._patterns:
            return
        min_idx = min(range(len(self._patterns)),
                      key=lambda i: self._patterns[i].salience)
        self._patterns.pop(min_idx)

    @staticmethod
    def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
        """余弦相似度"""
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-10 or nb < 1e-10:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
