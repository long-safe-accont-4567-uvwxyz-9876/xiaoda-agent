# memory/bridge_memory.py
"""桥接记忆: 跨会话连接语义相关但时间分散的记忆

源自 mazemaker dream_engine.py REM阶段
核心思想:
- sim在[0.3, 0.95)区间 → 真正的桥接 (相关但不重复)
- sim < 0.3 → 语义不相关
- sim >= 0.95 → 语义重复, 应走consolidation合并
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from loguru import logger


@dataclass
class BridgeMemory:
    """桥接记忆"""
    id: str
    source_memory_id: int
    target_memory_id: int
    weight: float
    bridge_type: str = "semantic"
    source_session_id: str = ""
    target_session_id: str = ""
    cross_session: bool = False
    discovered_at: float = field(default_factory=time.time)
    discovery_reason: str = "rem_bridge"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_memory_id": self.source_memory_id,
            "target_memory_id": self.target_memory_id,
            "weight": self.weight,
            "bridge_type": self.bridge_type,
            "source_session_id": self.source_session_id,
            "target_session_id": self.target_session_id,
            "cross_session": int(self.cross_session),
            "discovered_at": self.discovered_at,
            "discovery_reason": self.discovery_reason,
        }


class BridgeMemoryManager:
    """桥接记忆管理器

    REM桥接发现算法:
    1. 找孤立记忆 (linked < MAX_CONNECTIONS)
    2. 对每个orphan做cosine搜索 (k=10)
    3. sim在[0.3, 0.95)区间 → 桥接
    4. weight = similarity × BRIDGE_WEIGHT_FACTOR
    """

    SIM_THRESHOLD = 0.3
    SIM_HIGH = 0.95
    BRIDGE_WEIGHT_FACTOR = 0.3
    MAX_CONNECTIONS = 3

    async def discover_bridges(
        self,
        isolated_memories: list[Any],
        all_memories: list[Any],
        existing_connections: dict[int, set[int]] | None = None,
    ) -> list[BridgeMemory]:
        """发现桥接记忆

        Args:
            isolated_memories: 孤立记忆列表 (linked < MAX_CONNECTIONS)
            all_memories: 所有记忆 (用于搜索相似)
            existing_connections: 已有连接 {memory_id: {connected_ids}}

        Returns:
            发现的桥接记忆列表
        """
        if existing_connections is None:
            existing_connections = {}

        bridges: list[BridgeMemory] = []

        # 构建记忆embedding矩阵用于批量搜索
        all_embeddings = []
        all_ids = []
        for m in all_memories:
            if m.embedding is not None and m.embedding.size > 0:
                all_embeddings.append(m.embedding)
                all_ids.append(m.id)

        if not all_embeddings:
            return bridges

        # 过滤维度不一致的嵌入（模型切换后旧数据维度可能不同）
        ref_dim = all_embeddings[0].shape[0]
        dim_filtered = [(eid, emb) for eid, emb in zip(all_ids, all_embeddings) if emb.shape[0] == ref_dim]
        if not dim_filtered:
            return bridges
        all_ids = [eid for eid, _ in dim_filtered]
        all_embeddings = [emb for _, emb in dim_filtered]
        emb_matrix = np.stack(all_embeddings)

        for orphan in isolated_memories:
            if orphan.embedding is None or orphan.embedding.size == 0:
                continue

            # 检查是否孤立
            linked_count = len(orphan.linked) if hasattr(orphan, 'linked') else 0
            if linked_count >= self.MAX_CONNECTIONS:
                continue

            # 余弦搜索
            query = orphan.embedding
            query_norm = np.linalg.norm(query)
            if query_norm < 1e-10:
                continue

            emb_norms = np.linalg.norm(emb_matrix, axis=1)
            valid = emb_norms > 1e-10
            sims = np.zeros(len(all_embeddings))
            if valid.any():
                sims[valid] = np.dot(emb_matrix[valid], query) / (emb_norms[valid] * query_norm)

            # 取top-10
            top_indices = np.argsort(sims)[::-1][:10]

            existing = existing_connections.get(orphan.id, set())

            for idx in top_indices:
                target_id = all_ids[idx]
                similarity = float(sims[idx])

                # 桥接条件: sim在[0.3, 0.95)
                if similarity < self.SIM_THRESHOLD:
                    continue
                if similarity >= self.SIM_HIGH:
                    continue
                if target_id == orphan.id:
                    continue
                if target_id in existing:
                    continue

                # 查找target的session_id
                target = next((m for m in all_memories if m.id == target_id), None)
                target_session = getattr(target, 'session_id', '') if target else ''
                source_session = getattr(orphan, 'session_id', '')

                bridge = BridgeMemory(
                    id=str(uuid.uuid4()),
                    source_memory_id=orphan.id,
                    target_memory_id=target_id,
                    weight=similarity * self.BRIDGE_WEIGHT_FACTOR,
                    bridge_type="semantic",
                    source_session_id=source_session,
                    target_session_id=target_session,
                    cross_session=(source_session != target_session and bool(source_session) and bool(target_session)),
                    discovery_reason="rem_bridge",
                )
                bridges.append(bridge)
                existing.add(target_id)

        logger.info(f"BridgeMemory.discover: found {len(bridges)} bridges "
                     f"from {len(isolated_memories)} orphans")
        return bridges
