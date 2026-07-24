# core/conflict_supersession.py
"""冲突检测与超驱

源自 mazemaker dream_engine.py _phase_supersedes
核心洞察: 仅靠语义相似度不够
引入数值token差异作为冲突判据:
- cos_sim >= 0.85 (语义高度相似)
- numeric_tokens不同 (数值/金额/度量不同)
→ 判定为超驱关系
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np
from loguru import logger


@dataclass
class ConflictPair:
    """冲突记忆对"""
    old_memory_id: int
    new_memory_id: int
    old_timestamp: float
    new_timestamp: float
    similarity: float
    old_numeric_tokens: set[str]
    new_numeric_tokens: set[str]
    conflict_type: str = "numeric_token"


class ConflictSupersession:
    """冲突检测与超驱

    SUPERSEDES阶段:
    1. 对比记忆对: cos_sim >= 0.85 且 数值token不同
    2. old → new 有向边 (type=supersedes)
    3. 标记old为SUPERSEDED
    """

    SIMILARITY_THRESHOLD = 0.85

    # 数值token正则: 匹配数字+可选单位 (capture group → findall 返回纯数字部分)
    NUMERIC_PATTERN = re.compile(
        r'(\d+(?:\.\d+)?)(?:%|元|块|万|千|百|岁|年|月|天|小时|分钟|秒|km|m|cm|kg|g|GB|MB|TB|fps|hz|度|分|秒)?',
        re.IGNORECASE
    )

    async def detect_conflicts(self, memories: list[Any]) -> list[ConflictPair]:
        """检测冲突记忆对

        Args:
            memories: 记忆列表 (需有id, content, embedding, timestamp)

        Returns:
            冲突对列表
        """
        conflicts: list[ConflictPair] = []
        n = len(memories)

        if n < 2:
            return conflicts

        # 提取每条记忆的数值token
        numeric_tokens_map: dict[int, set[str]] = {}
        for m in memories:
            numeric_tokens_map[m.id] = self._extract_numeric_tokens(m.content)

        # O(n²) 两两比较
        for i in range(n):
            for j in range(i + 1, n):
                a, b = memories[i], memories[j]

                # 跳过无embedding的
                if (not hasattr(a, 'embedding') or a.embedding is None or a.embedding.size == 0
                        or not hasattr(b, 'embedding') or b.embedding is None or b.embedding.size == 0):
                    continue

                sim = self._cosine_sim(a.embedding, b.embedding)
                if sim < self.SIMILARITY_THRESHOLD:
                    continue

                # 检查数值token差异
                tokens_a = numeric_tokens_map[a.id]
                tokens_b = numeric_tokens_map[b.id]

                # 如果都有数值token且不同 → 冲突
                if tokens_a and tokens_b:
                    diff = tokens_a.symmetric_difference(tokens_b)
                    if diff:
                        # 按时间排序
                        if a.timestamp <= b.timestamp:
                            old, new = a, b
                        else:
                            old, new = b, a

                        conflicts.append(ConflictPair(
                            old_memory_id=old.id,
                            new_memory_id=new.id,
                            old_timestamp=old.timestamp,
                            new_timestamp=new.timestamp,
                            similarity=sim,
                            old_numeric_tokens=numeric_tokens_map[old.id],
                            new_numeric_tokens=numeric_tokens_map[new.id],
                        ))

        logger.info(f"ConflictSupersession.detect: found {len(conflicts)} conflicts "
                     f"from {n} memories")
        return conflicts

    async def apply_supersession(self, conflicts: list[ConflictPair]) -> int:
        """应用超驱 (标记old为SUPERSEDED)

        v0.6 桩实现：CognitiveMemory 当前为纯内存对象（无 DB 持久化），因此本方法
        仅做检测计数与日志记录，**不**实际修改记忆状态。真正的超驱动作将在 v0.7
        持久化阶段接入 DB 后实现，届时将完成以下三步：
          1. 标记 old_memory 的 status='SUPERSEDED'（episodic_memories.status 列）
          2. 向 memory_revisions 表写入一行修订记录（old→new + conflict_type）
          3. 在连接图中加入 type='supersedes' 的有向边

        Returns:
            检测到的冲突对数量（v0.6 不修改任何记忆状态）
        """
        logger.warning("ConflictSupersession.apply_supersession is a v0.6 stub — no memory state is modified")
        count = 0
        for conflict in conflicts:
            # TODO(v0.7): wire to DB — mark old_memory status=SUPERSEDED,
            #   write memory_revisions row, add type=supersedes edge
            logger.debug(f"Supersede: old={conflict.old_memory_id} → new={conflict.new_memory_id} "
                         f"sim={conflict.similarity:.3f} diff_tokens={conflict.old_numeric_tokens ^ conflict.new_numeric_tokens}")
            count += 1
        return count

    def _extract_numeric_tokens(self, content: str) -> set[str]:
        """提取内容中的数值token"""
        if not content:
            return set()
        return set(self.NUMERIC_PATTERN.findall(content))

    @staticmethod
    def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-10 or nb < 1e-10:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
