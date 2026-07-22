# core/dream_engine_v2.py
"""6阶段梦境整合引擎

源自 mazemaker dream_engine.py
阶段顺序: NREM → SUPERSEDES → REM → Insight → AFE/StageS → DAE

关键设计:
- 三切片采样: 50% recent + 30% random + 20% low_salience
  (对抗"表层陷阱": 旧记忆永远不被重放)
- NREM: Hebbian强化簇内连接 +0.05, 衰减簇外 -0.01, prune <0.05
- SUPERSEDES: cos≥0.85 + 数值token差异 → 有向边
- REM: 孤立记忆桥接发现
- Insight: Louvain社区检测 → 派生cluster摘要记忆
- AFE/StageS: 偏好结晶 (LLM蒸馏, ~10%产出率)
- DAE: 图感知嵌入 (邻居加权均值)
"""
from __future__ import annotations

import random
import time
from typing import Any

import numpy as np
from loguru import logger

from core.conflict_supersession import ConflictSupersession
from memory.bridge_memory import BridgeMemoryManager
from memory.cognitive_memory import CognitiveMemory, MemoryEntry
from memory.preference_discovery import PreferenceDiscovery
from memory.spreading_activation import SpreadingActivation


class DreamEngineV2:
    """6阶段梦境整合引擎"""

    IDLE_THRESHOLD = 600
    MEMORY_THRESHOLD = 50
    SAMPLE_LIMIT = 2000
    RECENT_PCT = 0.5
    RANDOM_OLD_PCT = 0.3
    LOW_SALIENCE_PCT = 0.2

    # NREM 参数
    NREM_STRENGTHEN_DELTA = 0.05
    NREM_WEAKEN_DELTA = 0.01
    PRUNE_THRESHOLD = 0.05

    # REM 参数
    REM_MAX_ISOLATED = 800
    REM_MAX_CONNECTIONS = 3

    # Insight 参数
    INSIGHT_MIN_COMMUNITY_SIZE = 4
    INSIGHT_MAX_CLUSTERS = 50

    # DAE 参数
    DAE_RECOMPUTE_EVERY = 5
    DAE_DAMPING = 0.7           # 保留 70% 原始嵌入, 30% 邻居加权均值
    DAE_MIN_NEIGHBORS = 2       # 至少 2 个邻居才更新

    # AFE/StageS 参数
    AFE_MAX_SESSIONS = 50       # 最多处理 50 条最近记忆
    AFE_SALIENCE = 2.0          # 偏好模式记忆的 salience

    def __init__(self, cognitive_memory: CognitiveMemory,
                 bridge_manager: BridgeMemoryManager | None = None,
                 spreading_activation: SpreadingActivation | None = None,
                 conflict_supersession: ConflictSupersession | None = None,
                 preference_discovery: PreferenceDiscovery | None = None,
                 llm_client: Any | None = None) -> None:
        self._cognitive = cognitive_memory
        self._bridge_mgr = bridge_manager or BridgeMemoryManager()
        self._spreading = spreading_activation or SpreadingActivation()
        self._conflict = conflict_supersession or ConflictSupersession()
        self._pref = preference_discovery or PreferenceDiscovery()
        self._llm_client = llm_client

        self._cycle_count = 0
        # 共享 CognitiveMemory 的连接图（引用，非拷贝），使 NREM Hebbian 强化能
        # 看到 consolidate() 中 self_attention_sweep 发现的连接。两者指向同一 dict，
        # 故 SUPERSEDES/REM 阶段写入的新边也对 CognitiveMemory 可见。
        self._connections = self._cognitive._connections
        self._last_dae_cycle = 0

    async def run_cycle(self) -> dict:
        """执行完整梦境周期"""
        t0 = time.time()
        self._cycle_count += 1

        stats = {
            "cycle": self._cycle_count,
            "nrem_sampled": 0,
            "nrem_strengthened": 0,
            "nrem_pruned": 0,
            "supersedes_found": 0,
            "rem_bridges": 0,
            "insight_communities": 0,
            "afe_patterns": 0,
            "dae_updated": 0,
            "duration_ms": 0.0,
        }

        try:
            # Phase 1: NREM
            nrem_stats = await self._phase_nrem()
            stats.update(nrem_stats)

            # Phase 2: SUPERSEDES
            sup_stats = await self._phase_supersedes()
            stats["supersedes_found"] = sup_stats.get("conflicts", 0)

            # Phase 3: REM
            rem_stats = await self._phase_rem()
            stats["rem_bridges"] = rem_stats.get("bridges", 0)

            # Phase 4: Insight
            insight_stats = await self._phase_insight()
            stats["insight_communities"] = insight_stats.get("communities", 0)

            # Phase 5: AFE/StageS (偏好结晶)
            afe_stats = await self._phase_afe_stage_s()
            stats["afe_patterns"] = afe_stats.get("patterns", 0)

            # Phase 6: DAE (每5个周期一次)
            if self._cycle_count - self._last_dae_cycle >= self.DAE_RECOMPUTE_EVERY:
                dae_stats = await self._phase_dae()
                stats["dae_updated"] = dae_stats.get("updated", 0)
                self._last_dae_cycle = self._cycle_count

        except Exception as e:
            logger.error(f"DreamEngineV2.run_cycle failed: {e}", exc_info=True)

        stats["duration_ms"] = (time.time() - t0) * 1000
        logger.info(f"DreamEngineV2 cycle {self._cycle_count} done: {stats}")
        return stats

    async def _phase_nrem(self) -> dict:
        """NREM: 强化+修剪"""
        # 1. 三切片采样
        all_memories = list(self._cognitive._episodic) + list(self._cognitive._semantic.values())
        if not all_memories:
            return {"nrem_sampled": 0, "nrem_strengthened": 0, "nrem_pruned": 0}

        sampled = self._sample_for_dream(all_memories, self.SAMPLE_LIMIT)

        # 2. 对每个seed做扩散激活, 强化簇内连接
        strengthened = 0
        for seed in sampled:
            seed_connections = self._connections.get(seed.id, {})
            for neighbor_id, weight in seed_connections.items():
                if weight > 0.3:  # 簇内
                    new_weight = min(1.0, weight + self.NREM_STRENGTHEN_DELTA)
                    self._connections[seed.id][neighbor_id] = new_weight
                    self._connections.setdefault(neighbor_id, {})[seed.id] = new_weight
                    strengthened += 1
                else:  # 簇外
                    new_weight = max(0.0, weight - self.NREM_WEAKEN_DELTA)
                    self._connections[seed.id][neighbor_id] = new_weight
                    self._connections.setdefault(neighbor_id, {})[seed.id] = new_weight

        # 3. 修剪弱连接
        pruned = 0
        for src_id in list(self._connections.keys()):
            for tgt_id in list(self._connections[src_id].keys()):
                if self._connections[src_id][tgt_id] < self.PRUNE_THRESHOLD:
                    del self._connections[src_id][tgt_id]
                    self._connections.get(tgt_id, {}).pop(src_id, None)
                    pruned += 1

        return {
            "nrem_sampled": len(sampled),
            "nrem_strengthened": strengthened,
            "nrem_pruned": pruned,
        }

    async def _phase_supersedes(self) -> dict:
        """SUPERSEDES: 冲突超驱"""
        all_memories = list(self._cognitive._episodic) + list(self._cognitive._semantic.values())
        conflicts = await self._conflict.detect_conflicts(all_memories)
        if conflicts:
            await self._conflict.apply_supersession(conflicts)
            # 写入连接图
            for c in conflicts:
                self._connections.setdefault(c.old_memory_id, {})[c.new_memory_id] = 0.9
        return {"conflicts": len(conflicts)}

    async def _phase_rem(self) -> dict:
        """REM: 桥接发现"""
        all_memories = list(self._cognitive._episodic) + list(self._cognitive._semantic.values())
        if not all_memories:
            return {"bridges": 0}

        # 找孤立记忆
        isolated = [m for m in all_memories
                    if len(self._connections.get(m.id, {})) < self.REM_MAX_CONNECTIONS]
        isolated = isolated[:self.REM_MAX_ISOLATED]

        if not isolated:
            return {"bridges": 0}

        existing = {mid: set(conns.keys()) for mid, conns in self._connections.items()}
        bridges = await self._bridge_mgr.discover_bridges(isolated, all_memories, existing)

        # 写入连接图
        for bridge in bridges:
            self._connections.setdefault(bridge.source_memory_id, {})[bridge.target_memory_id] = bridge.weight
            self._connections.setdefault(bridge.target_memory_id, {})[bridge.source_memory_id] = bridge.weight

        return {"bridges": len(bridges)}

    async def _phase_insight(self) -> dict:
        """Insight: 社区物化"""
        try:
            import networkx as nx
        except ImportError:
            logger.warning("networkx not available, skipping Insight phase")
            return {"communities": 0}

        if not self._connections:
            return {"communities": 0}

        # 构建NetworkX图
        g = nx.Graph()
        for src_id, conns in self._connections.items():
            for tgt_id, weight in conns.items():
                g.add_edge(src_id, tgt_id, weight=weight)

        # Louvain社区检测
        try:
            communities = nx.community.louvain_communities(g)
        except Exception:
            communities = [set(g.nodes())]

        # 派生cluster摘要记忆
        count = 0
        for community in communities:
            if len(community) < self.INSIGHT_MIN_COMMUNITY_SIZE:
                continue
            if count >= self.INSIGHT_MAX_CLUSTERS:
                break

            # 计算社区质心
            members = []
            for mid in community:
                m = self._cognitive._episodic_index.get(mid) or self._cognitive._semantic.get(mid)
                if m and m.embedding.size > 0:
                    members.append(m)

            if not members:
                continue

            centroid = np.mean([m.embedding for m in members], axis=0)
            centroid /= max(np.linalg.norm(centroid), 1e-10)

            # 存储为派生记忆
            representative = max(members, key=lambda m: m.access_count)
            await self._cognitive.remember(
                content=f"[cluster] {representative.content[:50]}",
                embedding=centroid,
                emotion_label="",
                label="cluster_summary",
            )
            count += 1

        return {"communities": count}

    async def _phase_afe_stage_s(self) -> dict:
        """AFE/StageS: 偏好结晶

        Stage C: 从最近记忆中提取用户状态事实
          - 有 LLM: 调用 LLM one-shot 提取
          - 无 LLM: 用记忆内容作为事实 (降级模式)
        Stage S: 聚类(cos>=0.85) + 蒸馏为高置信度模式
          - 10% 产出率 (有意为之, 更高产出率降低 recall 质量)
        """
        # 1. 收集最近记忆
        all_memories = list(self._cognitive._episodic) + list(self._cognitive._semantic.values())
        if not all_memories:
            return {"patterns": 0}

        # 按 timestamp 降序取最近的
        sorted_memories = sorted(all_memories, key=lambda m: m.timestamp, reverse=True)
        recent = sorted_memories[:self.AFE_MAX_SESSIONS]

        # 只取有内容和嵌入的
        valid = [m for m in recent if m.content and m.embedding.size > 0]
        if not valid:
            return {"patterns": 0}

        # 2. Stage C: 提取用户状态事实
        session_content = "\n".join(m.content for m in valid)
        facts = await self._pref.stage_c_extract(session_content, self._llm_client)

        if not facts:
            # 降级模式: 用记忆内容直接作为事实
            facts = [m.content for m in valid]

        if not facts:
            return {"patterns": 0}

        # 3. 准备嵌入矩阵 (用记忆的嵌入近似事实嵌入)
        # 每条 fact 对应一条源记忆的嵌入
        fact_embeddings = []
        fact_texts = []
        for i, fact in enumerate(facts):
            if i < len(valid):
                fact_embeddings.append(valid[i].embedding)
                fact_texts.append(fact)

        if len(fact_embeddings) < 2:
            return {"patterns": 0}

        emb_matrix = np.array(fact_embeddings, dtype=np.float32)

        # 4. Stage S: 聚类 + 蒸馏
        patterns = await self._pref.stage_s_synthesize(
            fact_texts, emb_matrix, self._llm_client
        )

        # 5. 存储为高 salience 记忆
        stored = 0
        for p in patterns:
            pattern_text = p.get("pattern_text", "")
            if not pattern_text:
                continue

            # 用聚类质心作为嵌入 (找最接近的源记忆嵌入)
            # 简化: 用第一条匹配记忆的嵌入
            pattern_emb = fact_embeddings[0].copy() if fact_embeddings else np.array([])

            await self._cognitive.remember(
                content=f"[preference] {pattern_text}",
                embedding=pattern_emb,
                emotion_label="",
                label="preference_pattern",
            )
            # 提升 salience
            # remember() 返回 mid, 但 MemoryEntry 的 salience 默认 1.0
            # 通过 access_count 间接提升优先级
            stored += 1

        logger.info(f"Phase 5 (AFE/StageS): {len(facts)} facts → "
                     f"{len(patterns)} patterns → {stored} stored")
        return {"patterns": stored}

    async def _phase_dae(self) -> dict:
        """DAE: 图感知嵌入更新

        对每条有连接的记忆, 用邻居的加权均值更新其嵌入:
          new_emb = damping * own_emb + (1-damping) * weighted_mean(neighbor_embs)

        - 邻居权重来自连接图中的 weight (Hebbian 强化后的值)
        - 仅在邻居数 >= DAE_MIN_NEIGHBORS 时更新
        - 嵌入归一化后写回
        """
        updated = 0

        # 收集所有记忆的查找表
        all_memories: dict[int, MemoryEntry] = {}
        for m in self._cognitive._episodic:
            all_memories[m.id] = m
        for mid, m in self._cognitive._semantic.items():
            all_memories[mid] = m

        for src_id, conns in self._connections.items():
            if len(conns) < self.DAE_MIN_NEIGHBORS:
                continue

            src_mem = all_memories.get(src_id)
            if not src_mem or src_mem.embedding.size == 0:
                continue

            # 收集邻居嵌入和权重
            neighbor_embs = []
            weights = []
            for tgt_id, w in conns.items():
                tgt_mem = all_memories.get(tgt_id)
                if tgt_mem and tgt_mem.embedding.size > 0 and w > 0:
                    neighbor_embs.append(tgt_mem.embedding)
                    weights.append(w)

            if len(neighbor_embs) < self.DAE_MIN_NEIGHBORS:
                continue

            # 计算加权均值
            emb_matrix = np.array(neighbor_embs, dtype=np.float32)
            weight_arr = np.array(weights, dtype=np.float32)
            weight_arr /= max(weight_arr.sum(), 1e-10)

            weighted_mean = np.average(emb_matrix, axis=0, weights=weight_arr)

            # 混合: damping * own + (1-damping) * neighbors
            own = src_mem.embedding.astype(np.float32)
            new_emb = self.DAE_DAMPING * own + (1.0 - self.DAE_DAMPING) * weighted_mean

            # 归一化
            norm = np.linalg.norm(new_emb)
            if norm > 1e-10:
                new_emb = new_emb / norm

            # 写回
            src_mem.embedding = new_emb.astype(src_mem.embedding.dtype)
            updated += 1

        logger.info(f"Phase 6 (DAE): updated {updated} embeddings "
                     f"(damping={self.DAE_DAMPING})")
        return {"updated": updated}

    def _sample_for_dream(self, memories: list[MemoryEntry],
                          limit: int = 2000) -> list[MemoryEntry]:
        """三切片采样: 50% recent + 30% random + 20% low_salience"""
        if not memories:
            return []

        n = min(limit, len(memories))
        recent_count = int(n * self.RECENT_PCT)
        random_count = int(n * self.RANDOM_OLD_PCT)
        low_sal_count = n - recent_count - random_count

        # 1. 最近切片 (按timestamp降序)
        sorted_by_time = sorted(memories, key=lambda m: m.timestamp, reverse=True)
        recent = sorted_by_time[:recent_count]
        recent_ids = {m.id for m in recent}

        # 2. 随机旧记忆切片
        remaining = [m for m in memories if m.id not in recent_ids]
        random.shuffle(remaining)
        random_old = remaining[:random_count]
        random_ids = {m.id for m in random_old}

        # 3. 低salience切片 (rescue)
        still_remaining = [m for m in memories
                           if m.id not in recent_ids and m.id not in random_ids]
        sorted_by_salience = sorted(still_remaining, key=lambda m: m.salience)
        low_salience = sorted_by_salience[:low_sal_count]

        result = recent + random_old + low_salience
        return result
