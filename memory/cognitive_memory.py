# memory/cognitive_memory.py
"""3层认知记忆管理器

⚠️ DEPRECATED: 本模块使用独立的衰减逻辑，与 FSRS-DSR 不兼容。
新代码应使用 memory.fsrs_model.FSRSModel 和 memory.confirm_correct 模块。

迁移路径:
  - CognitiveMemory.salience  → FSRSModel.retrievability()
  - CognitiveMemory.decay_factor → MemoryState.stability
  - CognitiveMemory.consolidate() → DreamConsolidator.consolidate_from_db()

预计移除版本: v0.5.0

Layer 1: EpisodicMemory — FIFO热缓冲 (内存)
Layer 2: SemanticMemory — 聚类长期存储 (内存, 后续持久化到SQLite)
Layer 3: HopfieldLayer — 联想记忆 (内存)

源自 mazemaker MemoryManager (src/memory/consolidation.cpp)
"""
from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

warnings.warn(
    "CognitiveMemory is deprecated. Use FSRSModel + DreamConsolidator instead.",
    DeprecationWarning,
    stacklevel=2,
)
from collections import deque

import numpy as np
from loguru import logger

from memory.hopfield_layer import HopfieldLayer
from memory.salience import SalienceScorer

# J-Space Hook: 结构化共享黑板 (非阻塞, 失败不影响主流程)
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS:
        from agent_core.structured_blackboard import StructuredBlackboard
        _structured_blackboard: "StructuredBlackboard | None" = None
    else:
        _structured_blackboard = None
except ImportError:
    _structured_blackboard = None


@dataclass
class MemoryEntry:
    """记忆条目 (对应 mazemaker MemoryEntry)"""
    id: int
    embedding: np.ndarray = field(default_factory=lambda: np.array([]))
    content: str = ""
    label: str = ""
    source: str = "perception"       # perception | inference | consolidated
    timestamp: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    salience: float = 1.0
    decay_factor: float = 1.0
    emotion_label: str = ""
    linked: list[int] = field(default_factory=list)
    session_id: str = ""

    def age_seconds(self, now: float) -> float:
        return now - self.timestamp

    def recency_seconds(self, now: float) -> float:
        return now - self.last_accessed


@dataclass
class Cluster:
    """语义聚类 (对应 mazemaker Cluster)"""
    id: int
    centroid: np.ndarray = None
    member_ids: list[int] = field(default_factory=list)
    coherence: float = 0.0
    created: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


class CognitiveMemory:
    """3层认知记忆管理器

    EpisodicMemory: FIFO deque, capacity=10000
    SemanticMemory: dict存储 + K-means聚类, max_clusters=256
    HopfieldLayer: Modern Hopfield联想, beta=20
    """

    AUTO_CONSOLIDATE_THRESHOLD = 0.8
    SALIENCE_TRANSFER_THRESHOLD = 0.3
    ACCESS_TRANSFER_THRESHOLD = 3
    CONNECTION_THRESHOLD = 0.5

    def __init__(self, dimensions: int = 512, episodic_capacity: int = 10000,
                 semantic_max_clusters: int = 256) -> None:
        self.dimensions = dimensions
        self.episodic_capacity = episodic_capacity
        self.semantic_max_clusters = semantic_max_clusters

        self._episodic: deque[MemoryEntry] = deque(maxlen=episodic_capacity)
        self._episodic_index: dict[int, MemoryEntry] = {}
        self._semantic: dict[int, MemoryEntry] = {}
        self._clusters: list[Cluster] = []
        self._connections: dict[int, dict[int, float]] = {}

        self._hopfield = HopfieldLayer(dimensions=dimensions)
        self._salience_scorer = SalienceScorer()
        self._next_episodic_id = 1
        self._next_semantic_id = 1000000
        self._next_cluster_id = 1

    async def remember(self, content: str, embedding: np.ndarray,
                       emotion_label: str = "", label: str = "",
                       session_id: str = "") -> int:
        """存储新记忆到Episodic层"""
        entry = MemoryEntry(
            id=self._next_episodic_id,
            embedding=embedding.astype(np.float32).copy(),
            content=content,
            label=label,
            source="perception",
            timestamp=time.time(),
            last_accessed=time.time(),
            emotion_label=emotion_label,
            session_id=session_id,
        )
        entry.salience = self._salience_scorer.compute(entry)

        self._episodic.append(entry)
        self._episodic_index[entry.id] = entry
        self._next_episodic_id += 1

        # J-Space Hook: 结构化存储
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS and _structured_blackboard is not None:
                await _structured_blackboard.put_structured(
                    str(entry.id), content, agent_name=session_id,
                    tags=["memory"], direction="memory")
        except Exception:
            pass

        # 自动整合检查
        if self.episodic_occupancy() > self.AUTO_CONSOLIDATE_THRESHOLD:
            await self.consolidate()

        return entry.id

    async def recall(self, query_embedding: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
        """混合检索: Episodic + Semantic + Hopfield"""
        query_embedding = query_embedding.astype(np.float32)
        results: dict[int, float] = {}

        # 1. Episodic 检索
        for entry in self._episodic:
            sim = self._cosine_sim(query_embedding, entry.embedding)
            results[entry.id] = sim

        # 2. Semantic 检索
        for sid, entry in self._semantic.items():
            sim = self._cosine_sim(query_embedding, entry.embedding)
            if sim > results.get(sid, 0):
                results[sid] = sim

        # 3. Hopfield 联想
        hop_result = self._hopfield.retrieve(query_embedding)
        if hop_result.confidence > 0.5:
            # 用Hopfield结果做二次检索
            for entry in list(self._episodic) + list(self._semantic.values()):
                sim = self._cosine_sim(hop_result.pattern, entry.embedding)
                if sim > results.get(entry.id, 0):
                    results[entry.id] = sim * hop_result.confidence

        # 排序取top-k
        sorted_results = sorted(results.items(), key=lambda x: x[1], reverse=True)
        return sorted_results[:k]

    async def consolidate(self, batch_size: int = 64) -> int:
        """认知整合: Episodic → Semantic + Hopfield"""
        now = time.time()

        # 1. 获取固化候选 (按access_count + age排序)
        candidates = sorted(
            self._episodic,
            key=lambda e: (e.access_count, -e.age_seconds(now)),
            reverse=True
        )[:batch_size]

        if not candidates:
            return 0

        # 2. 自注意力扫描发现关联
        connections = self.self_attention_sweep(candidates, self.CONNECTION_THRESHOLD)

        # 3. 转移高salience记忆
        transferred = 0
        episodic_ids_to_remove: set[int] = set()

        for entry in candidates:
            entry.salience = self._salience_scorer.compute(entry, now)
            if entry.salience > self.SALIENCE_TRANSFER_THRESHOLD or entry.access_count >= self.ACCESS_TRANSFER_THRESHOLD:
                # 转移到Semantic
                semantic_entry = MemoryEntry(
                    id=self._next_semantic_id,
                    embedding=entry.embedding.copy(),
                    content=entry.content,
                    label=entry.label,
                    source="consolidated",
                    timestamp=entry.timestamp,
                    last_accessed=now,
                    access_count=entry.access_count,
                    salience=entry.salience,
                    emotion_label=entry.emotion_label,
                    session_id=entry.session_id,
                )
                self._semantic[self._next_semantic_id] = semantic_entry
                self._next_semantic_id += 1

                # 存入Hopfield
                self._hopfield.store(entry.embedding, label=entry.label, source="consolidated")

                episodic_ids_to_remove.add(entry.id)
                transferred += 1

        # 4. 更新连接图
        for id_a, id_b, strength in connections:
            self._connections.setdefault(id_a, {})[id_b] = strength
            self._connections.setdefault(id_b, {})[id_a] = strength

        # 5. 从Episodic移除已转移记忆
        for mid in episodic_ids_to_remove:
            self._episodic_index.pop(mid, None)
        self._episodic = deque(
            (e for e in self._episodic if e.id not in episodic_ids_to_remove),
            maxlen=self.episodic_capacity
        )

        # 6. 重建Semantic聚类
        if transferred > 0:
            self._rebuild_clusters()

        logger.info(f"CognitiveMemory.consolidate: transferred={transferred} "
                     f"connections={len(connections)} episodic={len(self._episodic)} "
                     f"semantic={len(self._semantic)}")
        return transferred

    def connection_strength(self, a: MemoryEntry, b: MemoryEntry) -> float:
        """连接强度: sim×0.5 + temporal×0.3 + link_boost(max 0.3)"""
        if a.embedding.size == 0 or b.embedding.size == 0:
            return 0.0

        sim = self._cosine_sim(a.embedding, b.embedding)

        time_diff = abs(a.timestamp - b.timestamp)
        temporal_boost = math.exp(-time_diff / 60.0)  # 1分钟衰减

        link_boost = 0.0
        a_links = set(a.linked)
        for lid in b.linked:
            if lid in a_links:
                link_boost += 0.1
        link_boost = min(link_boost, 0.3)

        return max(0.0, sim * 0.5 + temporal_boost * 0.3 + link_boost)

    def self_attention_sweep(self, memories: list[MemoryEntry],
                             threshold: float = 0.5) -> list[tuple[int, int, float]]:
        """O(n²) 两两连接强度计算"""
        connections = []
        n = len(memories)
        for i in range(n):
            for j in range(i + 1, n):
                strength = self.connection_strength(memories[i], memories[j])
                if strength >= threshold:
                    connections.append((memories[i].id, memories[j].id, strength))
        connections.sort(key=lambda x: x[2], reverse=True)
        return connections

    def _touch(self, memory_id: int, count: int = 1) -> None:
        """更新访问计数"""
        entry = self._episodic_index.get(memory_id) or self._semantic.get(memory_id)
        if entry:
            entry.access_count += count
            entry.last_accessed = time.time()

    def episodic_size(self) -> int:
        return len(self._episodic)

    def semantic_size(self) -> int:
        return len(self._semantic)

    def episodic_occupancy(self) -> float:
        return len(self._episodic) / self.episodic_capacity

    def _rebuild_clusters(self) -> None:
        """重建Semantic聚类 (简单K-means)"""
        if not self._semantic:
            return

        entries = list(self._semantic.values())
        n = len(entries)
        k = min(self.semantic_max_clusters, max(1, n // 4))

        # 初始化聚类中心 (随机选k个)
        rng = np.random.default_rng(42)
        indices = rng.choice(n, min(k, n), replace=False)
        centroids = [entries[i].embedding.copy() for i in indices]

        # 迭代K-means (最多10次)
        for _ in range(10):
            clusters: list[list[int]] = [[] for _ in centroids]
            for entry in entries:
                best_idx = max(range(len(centroids)),
                               key=lambda i: self._cosine_sim(entry.embedding, centroids[i]))
                clusters[best_idx].append(entry.id)

            # 更新中心
            new_centroids = []
            for i, cluster_ids in enumerate(clusters):
                if cluster_ids:
                    cluster_entries = [self._semantic[mid] for mid in cluster_ids]
                    embeddings_list = [e.embedding for e in cluster_entries if e.embedding is not None and e.embedding.size > 0]
                    if embeddings_list:
                        ref_dim = embeddings_list[0].shape[0]
                        embeddings_list = [e for e in embeddings_list if e.shape[0] == ref_dim]
                        new_centroid = np.mean(embeddings_list, axis=0) if embeddings_list else centroids[i]
                    else:
                        new_centroid = centroids[i]
                    new_centroids.append(new_centroid)
                else:
                    new_centroids.append(centroids[i])
            centroids = new_centroids

        # 存储聚类
        self._clusters = []
        for i, cluster_ids in enumerate(clusters):
            if cluster_ids:
                self._clusters.append(Cluster(
                    id=self._next_cluster_id,
                    centroid=centroids[i],
                    member_ids=cluster_ids,
                    coherence=0.0,
                ))
                self._next_cluster_id += 1

    @staticmethod
    def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-10 or nb < 1e-10:
            return 0.0
        return float(np.dot(a, b) / (na * nb))