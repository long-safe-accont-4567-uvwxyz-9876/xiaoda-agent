# memory/preference_discovery.py
"""偏好结构发现: Stage C + Stage S

源自 mazemaker dream_engine.py AFE/StageS阶段

关键设计:
- Stage C: 从交互中提取用户状态事实 (LLM one-shot)
- Stage S: 聚类(cos>=0.85) + LLM蒸馏 → 高置信度模式记忆
- 10%低产出率是有意为之 (更高产出率反而降低recall质量)
"""
from __future__ import annotations

import json
import time
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

    STAGE_S_PROMPT = """以下是同一主题的一组用户偏好事实（语义相似，可能表述略有差异）。
请将它们提炼为一条最简洁、最准确、覆盖核心信息的偏好描述。

严格输出JSON，不要添加其他文字。格式：
{"pattern": "user prefers X"}

事实列表：
{cluster_text}"""

    # Stage C 输入长度上限（字符），避免本地小模型上下文溢出
    STAGE_C_MAX_CHARS = 6000
    # Stage S 单个聚类输入长度上限（字符）
    STAGE_S_MAX_CHARS = 4000

    async def stage_c_extract(self, session_content: str,
                              llm_client: Any | None = None) -> list[str]:
        """Stage C: LLM提取用户状态事实

        Args:
            session_content: 会话内容
            llm_client: LLM客户端 (FreeModelBackend 或兼容 .call(messages,...) 的对象；
                        None 时返回空列表)

        Returns:
            用户状态事实列表 ["user prefers X", ...]
        """
        if not llm_client or not session_content:
            return []

        try:
            # 防御性加固：session_content 可能含 {} 字符；截断避免上下文溢出
            content = session_content[:self.STAGE_C_MAX_CHARS]
            prompt = self.STAGE_C_PROMPT.replace("{session_content}", content)
            messages = [{"role": "user", "content": prompt}]
            raw = await self._call_llm(
                llm_client, messages, temperature=0.2, max_tokens=1024
            )
            if not raw:
                return []

            data = self._parse_json(raw)
            facts = data.get("facts") if isinstance(data, dict) else None
            if not isinstance(facts, list):
                return []
            return [str(f).strip() for f in facts if f and str(f).strip()]
        except Exception as e:
            logger.error("PreferenceDiscovery.stage_c failed: {}", e, exc_info=True)
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
            if not cluster_members:
                continue

            pattern_text: str | None = None
            confidence = 0.5
            if llm_client is not None:
                distilled = await self._distill_cluster(llm_client, cluster_members)
                if distilled:
                    pattern_text = distilled
                    confidence = 0.9

            if not pattern_text:
                # 无 LLM 或蒸馏失败时, 取 cluster 中最长的作为代表
                pattern_text = max(cluster_members, key=len)

            patterns.append({
                "pattern_text": pattern_text,
                "confidence": confidence,
                "salience": self.PATTERN_SALIENCE,
                "source_count": len(cluster_members),
            })

        logger.info("PreferenceDiscovery.stage_s: {} outputs → "
                     "{} clusters → {} patterns", len(stage_c_outputs),
                     len(clusters), len(patterns))
        return patterns

    async def _distill_cluster(self, llm_client: Any,
                               cluster_members: list[str]) -> str | None:
        """对单个聚类调用 LLM 蒸馏为一条偏好模式；失败返回 None。"""
        cluster_text = "\n".join(f"- {m}" for m in cluster_members)[:self.STAGE_S_MAX_CHARS]
        prompt = self.STAGE_S_PROMPT.replace("{cluster_text}", cluster_text)
        messages = [{"role": "user", "content": prompt}]
        raw = await self._call_llm(
            llm_client, messages, temperature=0.3, max_tokens=512
        )
        if not raw:
            return None

        data = self._parse_json(raw)
        pattern = data.get("pattern") if isinstance(data, dict) else None
        if not pattern:
            # 非 JSON 兜底：取首行作为模式文本
            lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
            pattern = lines[0] if lines else None
        return str(pattern).strip() if pattern else None

    async def _call_llm(self, llm_client: Any, messages: list[dict],
                        temperature: float = 0.3, max_tokens: int = 1024) -> str | None:
        """调用 LLM 客户端，兼容 FreeModelBackend.call / 通用 .chat / 直接可调用。"""
        if llm_client is None:
            return None

        call = getattr(llm_client, "call", None)
        if callable(call):
            try:
                result = await call(messages, temperature=temperature, max_tokens=max_tokens)
            except Exception as e:
                logger.debug("preference_discovery.call_failed", error=str(e)[:200])
                result = None
            return result if isinstance(result, str) else None

        chat = getattr(llm_client, "chat", None)
        if callable(chat):
            try:
                result = await chat(messages, temperature=temperature, max_tokens=max_tokens)
            except Exception as e:
                logger.debug("preference_discovery.chat_failed", error=str(e)[:200])
                result = None
            return result if isinstance(result, str) else None

        if callable(llm_client):
            try:
                result = await llm_client(messages, temperature=temperature, max_tokens=max_tokens)
            except Exception as e:
                logger.debug("preference_discovery.callable_failed", error=str(e)[:200])
                result = None
            return result if isinstance(result, str) else None

        return None

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """容错解析 LLM 返回的 JSON（剥离 markdown 代码块、提取首尾花括号）。"""
        if not raw or not raw.strip():
            return {}
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

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