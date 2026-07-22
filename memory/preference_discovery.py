# memory/preference_discovery.py
"""偏好结构发现: Stage C + Stage S

源自 mazemaker dream_engine.py AFE/StageS阶段

关键设计:
- Stage C: 从交互中提取用户状态事实 (LLM one-shot)
- Stage S: 聚类(cos>=0.85) + LLM蒸馏 → 高置信度模式记忆
- 10%低产出率是有意为之 (更高产出率反而降低recall质量)
"""
from __future__ import annotations

from typing import Any

import numpy as np
from loguru import logger


class PreferenceDiscovery:
    """偏好结构发现

    Stage C: LLM提取用户状态事实
    Stage S: 聚类 + LLM蒸馏为高置信度模式
    """

    CLUSTER_THRESHOLD = 0.85
    PATTERN_SALIENCE = 2.0
    YIELD_RATE = 0.10  # 10%产出率

    STAGE_C_PROMPT = """从以下对话内容中提取用户状态事实。
只提取明确的用户偏好、习惯、属性。

严格输出JSON，不要添加其他文字。格式：
{"facts": ["user prefers X", "user likes Y", "user does Z"]}

对话内容：
{session_content}"""

    async def stage_c_extract(self, session_content: str,
                              llm_client: Any | None = None) -> list[str]:
        """Stage C: LLM提取用户状态事实

        Args:
            session_content: 会话内容
            llm_client: LLM客户端 (None时返回空列表)

        Returns:
            用户状态事实列表 ["user prefers X", ...]
        """
        if not llm_client or not session_content:
            return []

        try:
            # 防御性加固：session_content 可能含 {} 字符
            _prompt = self.STAGE_C_PROMPT.replace("{session_content}", session_content)
            # 实际实现中调用LLM
            # response = await llm_client.chat(...)
            # return parse_json(response)["facts"]
            logger.warning("PreferenceDiscovery.stage_c_extract is a stub, returning []")
            return []
        except Exception as e:
            logger.error(f"PreferenceDiscovery.stage_c failed: {e}")
            return []

    async def stage_s_synthesize(self, stage_c_outputs: list[str],
                                 embeddings: np.ndarray | None = None,
                                 llm_client: Any | None = None) -> list[dict]:
        """Stage S: 聚类 + LLM蒸馏

        1. 按cos >= 0.85聚类Stage C输出
        2. 每个cluster LLM蒸馏为单一模式
        3. 存储为高置信度偏好记忆 (salience=2.0)
        """
        if not stage_c_outputs:
            return []

        if embeddings is None:
            # 无embedding时无法聚类, 返回空
            return []

        # 1. 聚类
        clusters = self._cluster_by_similarity(stage_c_outputs, embeddings, self.CLUSTER_THRESHOLD)

        # 2. 蒸馏 (10%产出率)
        patterns = []
        n_target = max(1, int(len(stage_c_outputs) * self.YIELD_RATE))

        for cluster_members in clusters[:n_target]:
            if not llm_client:
                # 无LLM时, 取cluster中最长的作为代表
                representative = max(cluster_members, key=len)
                patterns.append({
                    "pattern_text": representative,
                    "confidence": 0.5,
                    "salience": self.PATTERN_SALIENCE,
                    "source_count": len(cluster_members),
                })
            else:
                # 实际实现中调用LLM蒸馏
                pass

        logger.info(f"PreferenceDiscovery.stage_s: {len(stage_c_outputs)} outputs → "
                     f"{len(clusters)} clusters → {len(patterns)} patterns")
        return patterns

    def _cluster_by_similarity(self, outputs: list[str],
                               embeddings: np.ndarray,
                               threshold: float = 0.85) -> list[list[str]]:
        """按余弦相似度聚类

        Args:
            outputs: 文本列表
            embeddings: 对应的embedding矩阵 (n × dim)
            threshold: 聚类阈值

        Returns:
            聚类列表 (每个聚类是文本列表)
        """
        n = len(outputs)
        if n == 0:
            return []

        # 归一化
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms < 1e-10] = 1.0
        normalized = embeddings / norms

        # 计算相似度矩阵
        sim_matrix = normalized @ normalized.T

        # 贪心聚类
        assigned = [False] * n
        clusters: list[list[str]] = []

        for i in range(n):
            if assigned[i]:
                continue
            cluster = [outputs[i]]
            assigned[i] = True
            for j in range(i + 1, n):
                if not assigned[j] and sim_matrix[i, j] >= threshold:
                    cluster.append(outputs[j])
                    assigned[j] = True
            clusters.append(cluster)

        return clusters
