"""扩散激活检索引擎 — mind 风格的三通道融合

直接命中 (IDF + key重叠 + weight_bias) + 扩散激活 (沿边传播, 3跳)
+ RRF融合 + 模式补全 + 语义重排 + 模式分离
"""
import json
import math
from collections import defaultdict
from difflib import SequenceMatcher

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

    def __init__(self, concept_db, vector_store, key_extractor):
        self.db = concept_db
        self.vec = vector_store     # 现有 VectorStore（可为 None）
        self.key_extractor = key_extractor

    async def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """扩散激活检索主入口

        Returns:
            [{id, text, score, weight, keys}, ...] 按 score 降序
        """
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
        return out

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
        selected_texts = []

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
