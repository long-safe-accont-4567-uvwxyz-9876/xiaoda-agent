"""扩散激活检索引擎 — mind 风格的三通道融合

直接命中 (IDF + key重叠 + weight_bias) + 扩散激活 (沿边传播, 3跳)
+ RRF融合 + 模式补全 + 语义重排 + 模式分离

v0.6.0 新增: SpreadingActivation — 知识图谱扩散激活 (优先队列 + 链路预测)

Also provides: spread_activation — deterministic bounded spreading activation
for memory candidates (degree-penalized propagation over weighted edges).
"""
from __future__ import annotations

import heapq
import json
import math
import time
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from math import sqrt

import networkx as nx
from loguru import logger


class SpreadingActivationEngine:
    """扩散激活检索引擎"""

    # 参数（与 mind 一致，来自 spec）
    RECALL_RADIUS = 3           # 最大扩散跳数
    ACTIVATION_DECAY = 0.5      # 每跳衰减50%
    SPREADING_THRESHOLD = 0.05  # 低于不传播
    RRF_K = 60                  # RRF 平滑参数
    FUZZY_ACTIVATION = 0.5     # 模糊匹配系数
    SEPARATION_SIM = 0.92      # 去重相似度阈值

    # G13: recall 结果 LRU+TTL 缓存参数
    RECALL_CACHE_MAXSIZE = 256  # 最大缓存条目
    RECALL_CACHE_TTL = 300      # 5 分钟

    def __init__(self, concept_db, vector_store, key_extractor):
        self.db = concept_db
        self.vec = vector_store     # 现有 VectorStore（可为 None）
        self.key_extractor = key_extractor
        # G13: recall 结果 LRU+TTL 缓存
        # OrderedDict[cache_key=(query, top_k)] -> (expiry_monotonic, list[dict])
        self._recall_cache: "OrderedDict[tuple, tuple[float, list[dict]]]" = OrderedDict()

    async def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """扩散激活检索主入口

        Returns:
            [{id, text, score, weight, keys}, ...] 按 score 降序

        G13: 按 (query, top_k) 缓存结果，TTL 5 分钟，maxsize 256。
        """
        # G13: 缓存命中检查
        cache_key = (query, top_k)
        now = time.monotonic()
        cached_entry = self._recall_cache.get(cache_key)
        if cached_entry is not None:
            expiry, cached = cached_entry
            if now < expiry:
                # 命中：移到末尾（LRU 优先级最高），返回深拷贝避免污染缓存
                self._recall_cache.move_to_end(cache_key)
                return [dict(item) for item in cached]
            # 过期：清除条目
            del self._recall_cache[cache_key]

        # Step 1: Key 提取
        query_keys = set(self.key_extractor.extract(query, is_query=True))
        if not query_keys:
            return []

        # Step 2: 获取存活节点
        alive_nodes = await self.db.get_alive_nodes()
        if not alive_nodes:
            return []

        # Step 3: IDF 计算
        idf = self._compute_idf(query_keys, alive_nodes)

        # Step 4: 直接命中通道
        direct = self._direct_channel(query_keys, idf, alive_nodes, query)

        # Step 5: 模式补全（无直接命中时用向量模糊匹配）
        if not direct:
            direct = await self._pattern_completion(query, alive_nodes)
        if not direct:
            return []

        # Step 6: 扩散激活通道
        spread = await self._spreading_channel(direct, alive_nodes)

        # Step 7: RRF 融合
        fused = self._rrf_fusion(direct, spread)

        # Step 8: 语义重排
        fused = await self._semantic_rerank(query, fused, top_k)

        # Step 9: 模式分离（去重）
        results = self._pattern_separation(fused, top_k)

        # 填充完整字段
        out = []
        for item in results:
            node = alive_nodes.get(item["id"], {})
            out.append({
                "id": item["id"],
                "text": node.get("text", ""),
                "score": item["score"],
                "weight": node.get("weight", 1.0),
                "keys": node.get("keys", "[]"),
            })

        # G13: 写入缓存（存深拷贝避免外部修改污染）
        self._recall_cache[cache_key] = (
            now + self.RECALL_CACHE_TTL,
            [dict(item) for item in out],
        )
        # LRU 淘汰：超过 maxsize 时弹出最旧条目
        while len(self._recall_cache) > self.RECALL_CACHE_MAXSIZE:
            self._recall_cache.popitem(last=False)

        return out

    def clear_cache(self) -> None:
        """G13: 清空 recall 缓存（记忆写入后调用）。"""
        self._recall_cache.clear()

    def _compute_idf(self, keys: set, alive_nodes: dict) -> dict:
        """计算每个 key 的 IDF 值

        idf(k) = log(N / (1 + df(k)))
        N = 存活节点总数, df(k) = 包含 key k 的节点数
        """
        n = len(alive_nodes)
        if n == 0:
            return {}
        df = defaultdict(int)
        for node in alive_nodes.values():
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                continue
            for k in keys & node_keys:
                df[k] += 1
        return {k: math.log(n / (1 + df.get(k, 0))) for k in keys}

    def _direct_channel(self, keys: set, idf: dict,
                         alive_nodes: dict, query: str) -> dict:
        """IDF 加权 key 重叠 + 子串包含

        weight_bias = 0.35 + 0.65 * weight（floor 0.35）
        """
        direct = {}
        q_lower = query.lower()
        for nid, node in alive_nodes.items():
            try:
                node_keys = set(json.loads(node.get("keys", "[]")))
            except (json.JSONDecodeError, TypeError):
                node_keys = set()
            # weight_bias floor 0.35
            w_bias = 0.35 + 0.65 * node.get("weight", 1.0)

            shared = keys & node_keys
            if shared:
                idf_score = sum(idf.get(k, 0) for k in shared)
                direct[nid] = direct.get(nid, 0) + idf_score * w_bias

            # 子串包含（len >= 4 才计）
            n_text = node.get("text", "").lower()
            substr = sum(1 for w in keys if len(w) >= 4 and w in n_text)
            reverse = sum(1 for k in node_keys
                           if len(k) >= 4 and k in q_lower)
            if substr + reverse:
                direct[nid] = direct.get(nid, 0) + (substr + reverse) * 0.6 * w_bias

        return direct

    async def _pattern_completion(self, query: str,
                                    alive_nodes: dict) -> dict:
        """无直接命中时，用现有 VectorStore 做模糊匹配

        复用 VectorStore.search() 的向量检索能力，
        将结果映射到 concept_nodes（通过 source_mem_id）。
        """
        direct = {}
        if not self.vec or not getattr(self.vec, "enabled", False):
            return direct
        try:
            vec_results = await self.vec.search(query, top_k=20)
        except Exception as e:
            logger.debug("spreading.pattern_completion_failed", error=str(e))
            return direct
        if not vec_results:
            return direct
        for result in vec_results:
            # vec_results 可能是 list[tuple(id, distance)] 或 list[dict]
            if isinstance(result, (list, tuple)):
                row_id, distance = result[0], result[1]
            else:
                row_id = result.get("id")
                distance = result.get("distance", 1.0)
            node = await self.db.get_node_by_source_mem(row_id)
            if node and node["id"] in alive_nodes:
                sim = max(0.0, 1.0 - distance)
                if sim >= 0.25:
                    direct[node["id"]] = (sim * self.FUZZY_ACTIVATION
                                           * node.get("weight", 1.0))
        return direct

    async def _spreading_channel(self, direct: dict,
                                  alive_nodes: dict) -> dict:
        """从种子节点沿边传播激活值，3跳"""
        spread = defaultdict(float)
        wave = dict(direct)

        for hop in range(self.RECALL_RADIUS + 1):
            nxt = defaultdict(float)
            for nid, act in wave.items():
                spread[nid] += act  # 累积激活
                if hop < self.RECALL_RADIUS and act > self.SPREADING_THRESHOLD:
                    edges = await self.db.get_edges(nid)
                    for neighbor_id, edge in edges.items():
                        if neighbor_id not in alive_nodes:
                            continue  # closed 事实不中继
                        propagated = (act * self.ACTIVATION_DECAY
                                      * edge["weight"] / (hop + 1))
                        nxt[neighbor_id] += propagated
            wave = nxt
            if not wave:
                break

        return dict(spread)

    def _rrf_fusion(self, direct: dict, spread: dict) -> dict:
        """Reciprocal Rank Fusion: 双通道排名融合"""
        dr = {n: i for i, (n, _) in enumerate(
            sorted(direct.items(), key=lambda x: (-x[1], x[0])))}
        sr = {n: i for i, (n, _) in enumerate(
            sorted(spread.items(), key=lambda x: (-x[1], x[0])))}
        dr_default = len(dr) + 1
        sr_default = len(sr) + 1

        fused = {}
        for nid in set(direct) | set(spread):
            fused[nid] = (1.0 / (self.RRF_K + dr.get(nid, dr_default)) +
                          1.0 / (self.RRF_K + sr.get(nid, sr_default)))
        return fused

    async def _semantic_rerank(self, query: str, fused: dict,
                                 top_k: int) -> list:
        """语义重排：用文本相似度对 fused 结果重排"""
        if not fused:
            return []
        # 取 fused 分数 top candidates
        sorted_items = sorted(fused.items(), key=lambda x: (-x[1], x[0]))
        candidates = sorted_items[:top_k * 3]  # 过采样

        # 计算文本相似度
        reranked = []
        for nid, rrf_score in candidates:
            node = await self.db.get_node(nid)
            if not node:
                continue
            text_sim = SequenceMatcher(
                None, query.lower(), node.get("text", "").lower()
            ).ratio()
            # 综合分数 = RRF + 文本相似度
            combined = rrf_score + text_sim * 0.1
            reranked.append({"id": nid, "score": combined})

        reranked.sort(key=lambda x: -x["score"])
        return reranked[:top_k * 2]  # 留余量给 pattern_separation

    def _pattern_separation(self, fused_or_list, top_k: int) -> list:
        """模式分离：相似文本去重

        Args:
            fused_or_list: dict {id: score} 或 list[{id, score}]
            top_k: 返回数量上限
        """
        # 统一转为 list[{id, score}]
        if isinstance(fused_or_list, dict):
            items = [{"id": nid, "score": s}
                     for nid, s in fused_or_list.items()]
            items.sort(key=lambda x: -x["score"])
        else:
            items = list(fused_or_list)

        if not items:
            return []

        # 逐个检查是否与已选结果过于相似
        selected = []

        for item in items:
            if len(selected) >= top_k:
                break
            # 获取节点文本（从 alive_nodes 或 db）
            # 这里只比较已知文本，无法获取则保留
            # 由于此方法可能在没有 node 文本的情况下调用，
            # 我们简化为基于 id 去重（文本去重在上层处理）
            if item["id"] not in [s["id"] for s in selected]:
                selected.append(item)

        return selected


# ──────────────────────────────────────────────────────────────────
# v0.6.0: SpreadingActivation — 知识图谱扩散激活
# 源自 mazemaker graph.h KnowledgeGraph::spread_activation
# 算法: 优先队列扩散, activation[seed]=1.0, propagated = act × edge.weight × decay
# ──────────────────────────────────────────────────────────────────


@dataclass
class TraversalResult:
    """遍历结果"""
    node_id: int
    activation: float
    depth: int
    path: list[int] = field(default_factory=list)


@dataclass
class ConnectionPrediction:
    """连接预测"""
    source_id: int
    target_id: int
    confidence: float
    method: str = "common_neighbors"


class SpreadingActivation:
    """扩散激活

    从种子节点出发, 沿边传播激活值:
    - activation[seed] = 1.0
    - propagated = act × edge.weight × decay
    - 低于threshold的停止传播
    - 超过max_depth的停止传播
    """

    DECAY = 0.85
    THRESHOLD = 0.01
    MAX_DEPTH = 5

    def spread(self, graph: nx.Graph, seed_id: int,
               decay: float = 0.85, threshold: float = 0.01,
               max_depth: int = 5) -> list[TraversalResult]:
        """从种子节点扩散激活

        Args:
            graph: NetworkX图 (边需有weight属性)
            seed_id: 种子节点ID
            decay: 衰减因子 (0~1)
            threshold: 激活阈值
            max_depth: 最大传播深度

        Returns:
            激活的节点列表 (按激活值降序)
        """
        if seed_id not in graph:
            return []

        activation: dict[int, float] = {seed_id: 1.0}
        depth: dict[int, int] = {seed_id: 0}
        # 优先队列: (-activation, node_id)  负号因为heapq是最小堆
        queue: list[tuple[float, int]] = [(-1.0, seed_id)]
        visited: set[int] = set()

        results: list[TraversalResult] = []

        while queue:
            neg_act, current = heapq.heappop(queue)
            act = -neg_act

            if current in visited:
                continue
            visited.add(current)

            if act < threshold:
                continue

            results.append(TraversalResult(
                node_id=current,
                activation=act,
                depth=depth.get(current, 0),
            ))

            # 达到最大深度则不再继续传播
            if depth.get(current, 0) >= max_depth:
                continue

            # 扩散到邻居
            for neighbor in graph.neighbors(current):
                if neighbor in visited:
                    continue
                edge_data = graph.get_edge_data(current, neighbor)
                edge_weight = edge_data.get('weight', 0.5) if edge_data else 0.5
                propagated = act * edge_weight * decay

                if propagated > activation.get(neighbor, 0):
                    activation[neighbor] = propagated
                    depth[neighbor] = depth.get(current, 0) + 1
                    heapq.heappush(queue, (-propagated, neighbor))

        results.sort(key=lambda r: r.activation, reverse=True)
        return results

    def predict_links(self, graph: nx.Graph, node_id: int,
                      max_results: int = 10) -> list[ConnectionPrediction]:
        """链路预测

        融合三种方法:
        score = 0.3 × common_neighbors + 0.4 × adamic_adar + 0.3 × (1.0 固定, 无embedding时)
        """
        if node_id not in graph:
            return []

        predictions: list[ConnectionPrediction] = []
        neighbors = set(graph.neighbors(node_id))

        for candidate in graph.nodes():
            if candidate == node_id or candidate in neighbors:
                continue

            # Common neighbors
            cn_score = len(neighbors & set(graph.neighbors(candidate)))

            # Adamic-Adar
            aa_score = 0.0
            for common in neighbors & set(graph.neighbors(candidate)):
                degree = graph.degree(common)
                if degree > 1:
                    aa_score += 1.0 / math.log(degree)

            # 组合分数 (无embedding时用固定0.5)
            combined = 0.3 * cn_score + 0.4 * aa_score + 0.3 * 0.5

            if combined > 0:
                predictions.append(ConnectionPrediction(
                    source_id=node_id,
                    target_id=candidate,
                    confidence=combined,
                    method="common_neighbors+adamic_adar",
                ))

        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions[:max_results]


# ──────────────────────────────────────────────────────────────────
# Deterministic bounded spreading activation for memory candidates.
# Used by evaluation/memory_benchmark.py and KG v2 search pipeline.
# ──────────────────────────────────────────────────────────────────


def spread_activation(
    seed_scores: Mapping[str, float],
    adjacency: Mapping[str, Mapping[str, float]],
    max_hops: int = 1,
    decay: float = 0.5,
    threshold: float = 0.05,
    candidate_budget: int = 50,
) -> dict[str, float]:
    """Return seed scores plus activation propagated over weighted edges."""
    scores = {node: float(score) for node, score in seed_scores.items()}
    frontier = dict(scores)

    for hop in range(1, max(0, max_hops) + 1):
        next_frontier: dict[str, float] = {}
        for source, activation in sorted(frontier.items()):
            neighbors = adjacency.get(source, {})
            degree_penalty = 1.0 / sqrt(max(1, len(neighbors)))
            for target, weight in sorted(neighbors.items()):
                propagated = (
                    float(activation)
                    * float(weight)
                    * decay
                    * degree_penalty
                    / hop
                )
                if propagated >= threshold:
                    next_frontier[target] = next_frontier.get(target, 0.0) + propagated
        for target, activation in next_frontier.items():
            scores[target] = scores.get(target, 0.0) + activation
        frontier = next_frontier
        if not frontier:
            break

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[: max(0, candidate_budget)])
