from typing import Any, Optional
import asyncio
import re
import time
from loguru import logger

from db.database import DatabaseManager
from db.db_memory import MemoryDB
# FTS5 分词工具从 db.fts_utils 导入 (打破 db <-> memory 循环); 这里 re-export
# 保持向后兼容 (其他模块仍可 `from memory.memory_manager import _tokenize_for_fts`)
from db.fts_utils import (
    _tokenize_for_fts,
    _extract_fts_keywords,
    _build_fts_query,
)
from .vector_store import VectorStore
from .fluid_memory import FluidMemory
from .memory_distiller import MemoryDistiller
from utils.atomic_write import atomic_json_write
from config import get_agent_display_name


def _extract_entities(text: str) -> list[str]:
    try:
        import jieba
        words = jieba.cut(text)
        return [w for w in words if len(w) >= 2]
    except ImportError:
        return [text[i:i+n] for n in range(2, 5) for i in range(len(text)-n+1)]


# ── 时间实体识别（解析"昨天/前天/上周/N天前"等中文时间词）──
import datetime as _datetime

# 时间词 → 相对今天的偏移天数（offset_days, span_days）
# offset_days: 起点距今多少天前；span_days: 时间跨度
# 注意: "大前天" 必须排在 "前天" 之前，因 "大前天" 包含 "前天" 子串，
# _parse_temporal_query 在首个命中即返回，顺序错误会导致 "大前天" 被误判为 "前天"。
_TEMPORAL_PATTERNS = [
    (re.compile(r"大前天"), 3, 1),             # 大前天那一天（3天前）
    (re.compile(r"前天"), 2, 1),               # 前天那一天（2天前）
    (re.compile(r"昨天|昨日"), 1, 1),          # 昨天那一天
    (re.compile(r"今天|今日"), 0, 1),          # 今天
    (re.compile(r"上周"), 7, 7),               # 上周（7-14天前那一周）
    (re.compile(r"上个月|上月"), 30, 30),      # 上个月
    (re.compile(r"前几天|前些天|最近"), 1, 7), # 最近一周
]


def _parse_temporal_query(query: str) -> tuple[float, float] | None:
    """从用户查询中解析时间词，返回 [start_ts, end_ts] 时间戳区间（秒）。

    支持的词：昨天/前天/大前天/今天/上周/上个月/前几天/最近
    无时间词返回 None。

    注意：这是一个轻量规则解析器，不调 LLM，毫秒级。
    """
    for pattern, offset_days, span_days in _TEMPORAL_PATTERNS:
        if pattern.search(query):
            now = _datetime.datetime.now()
            # 计算起始日期的 00:00:00
            start_date = (now - _datetime.timedelta(days=offset_days + span_days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # 结束日期的 23:59:59（用次日 00:00:00 表示开区间）
            end_date = (now - _datetime.timedelta(days=offset_days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) if offset_days > 0 else now
            return start_date.timestamp(), end_date.timestamp()
    return None


# 停用词集合（话题关键词提取时过滤）
# 注意：agent 显示名（如"小妲"）在 _extract_topic_keywords 中动态注入，
# 以确保用户自定义 display_name 后仍能被正确过滤
_TOPIC_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "和", "与", "或", "但", "如果", "因为", "所以", "虽然", "不过", "然后",
    "这", "那", "这个", "那个", "这些", "那些", "什么", "怎么", "为什么", "哪",
    "有", "没有", "不", "没", "可以", "能", "会", "要", "想", "觉得", "感觉",
    "就", "都", "也", "还", "又", "只", "才", "已经", "正在", "一直",
    "吗", "呢", "吧", "啊", "哦", "嗯", "呀", "哈", "嘿",
    "用户", "助手", "人家", "爸爸", "妈妈",
}


def _get_topic_stopwords() -> set:
    """返回带当前 agent display_name 的停用词集合。"""
    return _TOPIC_STOPWORDS | {get_agent_display_name("xiaoda")}


def _extract_topic_keywords(query: str, top_n: int = 2) -> list[str]:
    """从用户查询中抽取话题关键词（用于主动联想检索）。

    优先用 jieba.extract_tags，降级到 jieba.cut + 过滤停用词。
    返回 top_n 个关键词，每个长度 >= 2。
    """
    # 去除时间词（已被 _parse_temporal_query 处理）
    for pattern, _, _ in _TEMPORAL_PATTERNS:
        query = pattern.sub("", query)
    query = query.strip()
    if not query:
        return []

    try:
        import jieba.analyse
        keywords = jieba.analyse.extract_tags(
            query, topK=top_n * 2, withWeight=False, allowPOS=("n", "nr", "ns", "nt", "nz", "vn", "v", "eng", "a", "ad", "an")
        )
        # 过滤停用词和过短的词
        stopwords = _get_topic_stopwords()
        keywords = [kw for kw in keywords if len(kw) >= 2 and kw not in stopwords]
        return keywords[:top_n]
    except (ImportError, OSError):
        # 降级到普通分词
        try:
            import jieba
            words = jieba.lcut(query)
            stopwords = _get_topic_stopwords()
            words = [w for w in words if len(w) >= 2 and w not in stopwords]
            return words[:top_n]
        except (ImportError, OSError):
            return []


def reciprocal_rank_fusion(ranked_lists: list[list[str]], *, k: int = 60, limit: int = 10,
                           weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: 多路排序融合算法

    Args:
        ranked_lists: 多路排序结果 (每路是 id 列表, 按相关性降序)
        k: 平滑常数 (标准值 60), 防止排名 1 的项压倒一切
        limit: 返回前 N 个
        weights: 各通道权重 (长度须与 ranked_lists 一致)。
            None 或全等值时退化为等权 RRF (向后兼容)。
            空列表通道不参与融合, 自动置零 (空通道熔断)。
    """
    scores: dict[str, float] = {}
    for i, ranked in enumerate(ranked_lists):
        if not ranked:
            continue  # 空通道自动跳过, 不稀释有效候选
        w = weights[i] if weights and i < len(weights) else 1.0
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + w * 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]


class RuleBasedMemoryExtractor:
    """基于正则的即时记忆提取器"""

    _PATTERNS: list[tuple[str, re.Pattern, float, float]] = [
        ("memory_request", re.compile(r'请?记住|记一下|帮我记|remember|别忘了|要记得', re.I), 0.95, 0.8),
        ("preference", re.compile(r'我更?喜欢|偏好|倾向|希望|不喜欢|讨厌|以后请?默认?|prefer|我习惯', re.I), 0.78, 0.7),
        ("decision", re.compile(r'决定|确定|确认|采用|选用|改成|规划|方案|we decided|就选', re.I), 0.78, 0.7),
        ("task", re.compile(r'下一步|之后|稍后|待办|TODO|需要做|要做|follow up|记得要', re.I), 0.68, 0.55),
        ("assistant_decision", re.compile(r'好的，我会|已为你|已设置|已修改|已创建', re.I), 0.6, 0.5),
    ]

    def extract(self, user_message: str, assistant_message: str = "") -> list[dict]:
        results = []
        for kind, pattern, confidence, importance in self._PATTERNS:
            text = user_message if kind != "assistant_decision" else assistant_message
            if pattern.search(text):
                results.append({
                    "kind": kind,
                    "confidence": confidence,
                    "importance": importance,
                    "source": "rule",
                })
        return results


def validate_memory_content(content: str) -> str | None:
    """验证记忆内容安全性，返回拒绝原因或 None（通过）"""
    if not content or not content.strip():
        return "empty_content"
    lower = content.lower()
    sensitive_patterns = [
        'api_key', 'apikey', 'api-key', 'authorization', 'bearer ',
        'cookie', 'password', 'private key', 'secret_key', 'secret-key',
        'access_token', 'refresh_token',
    ]
    for pattern in sensitive_patterns:
        if pattern in lower:
            return f"sensitive_keyword:{pattern}"
    if ';base64,' in lower or 'data:image/' in lower:
        return "base64_or_data_uri"
    if 'signature=' in lower and ('http://' in lower or 'https://' in lower):
        return "signed_url"
    return None


def _normalize_for_dedupe(text: str) -> str:
    """归一化文本用于去重：移除所有空白+小写"""
    import re as _re
    return _re.sub(r'\s+', '', text).casefold()


class MemoryManager:

    IDLE_THRESHOLD = 30
    ENCODE_COOLDOWN = 60

    def __init__(self, db: DatabaseManager, memory: MemoryDB,
                 vector_store: VectorStore | None = None,
                 router: Optional[Any]=None, knowledge_graph: Optional[Any]=None, security_filter: Optional[Any]=None,
                 reranker: Optional[Any]=None, query_transformer: Optional[Any]=None,
                 governance: Optional[Any]=None) -> None:
        self.db = db
        self.memory = memory
        self.vec = vector_store
        self.router = router
        self.kg = knowledge_graph
        self._security_filter = security_filter
        self._reranker = reranker
        self._query_transformer = query_transformer
        self._governance = governance
        self._last_message_time: float = 0
        self._last_encode_time: float = 0
        self._pending_encode = False
        # P3 记忆蒸馏器（使用硅基流动免费模型，失败降级到 router）
        self.distiller = MemoryDistiller(router=router)
        # 冷启动路由: 记忆计数缓存 (TTL 60s, 避免每次检索都 COUNT 全表)
        self._memory_count_cache: int | None = None
        self._memory_count_ts: float = 0

    def set_knowledge_graph(self, kg: Any) -> None:
        self.kg = kg

    def set_governance(self, governance: Any) -> None:
        """注入 ContextGovernance 实例 (ContextNest 哈希链 + 审计追踪)。"""
        self._governance = governance

    # ── 冷启动路由: 记忆计数 + 档位判断 ──────────────────────────
    async def _get_memory_count(self) -> int:
        """获取用户私有记忆总数 (带 60s TTL 缓存, 避免频繁 COUNT)."""
        now = time.time()
        if self._memory_count_cache is not None and (now - self._memory_count_ts) < 60:
            return self._memory_count_cache
        try:
            count = await self.memory.get_episodic_count()
        except (OSError, TypeError):
            count = 0
        self._memory_count_cache = count
        self._memory_count_ts = now
        return count

    def invalidate_memory_count_cache(self) -> None:
        """写入新记忆后主动失效缓存, 下次检索立即感知."""
        self._memory_count_cache = None
        self._memory_count_ts = 0

    async def get_memory_tier(self) -> str:
        """判断当前用户记忆档位: "cold" / "warm" / "hot".

        cold (0~COLD_MAX):       纯 FTS, 向量检索完全关闭
        warm (COLD_MAX+1~WARM_MAX): 向量低权重参与, 以关键词为主
        hot  (>WARM_MAX):        BM25+向量均衡融合
        """
        try:
            import config as _cfg
            cold_max = getattr(_cfg, "MEMORY_COLD_MAX", 0)
            warm_max = getattr(_cfg, "MEMORY_WARM_MAX", 10)
        except (ImportError, AttributeError):
            cold_max, warm_max = 0, 10
        count = await self._get_memory_count()
        if count <= cold_max:
            return "cold"
        if count <= warm_max:
            return "warm"
        return "hot"

    async def audit_retrieval(self, response_id: str,
                                memories: list[dict] | None) -> int:
        """ContextNest A2: 审计一次检索消费了哪些记忆版本。

        由调用方 (message_processor) 在 retrieve_memories 返回后显式调用,
        记录 (response_id, memory_id, content_hash, version, score, source) 到
        context_audit_log, 支持 point-in-time 重建。
        """
        if not self._governance or not memories:
            return 0
        try:
            return await self._governance.audit_context_consumption(
                response_id, memories, auto_commit=True,
            )
        except Exception as e:
            logger.debug("memory.audit_retrieval_failed", error=str(e))
            return 0

    async def _has_duplicate(self, summary: str) -> bool:
        """检查是否存在归一化后内容相同的已有记忆"""
        normalized = _normalize_for_dedupe(summary)
        if len(normalized) < 10:
            return False
        try:
            # 用 FTS 搜索相关记忆，然后精确匹配
            candidates = await self.memory.search_memories_fts(summary, limit=5)
            for c in candidates:
                if _normalize_for_dedupe(c.get("summary", "")) == normalized:
                    return True
            # FTS 无结果时也检查最近记忆
            recent = await self.memory.get_episodic_recent(limit=10)
            for r in recent:
                if _normalize_for_dedupe(r.get("summary", "")) == normalized:
                    return True
        except (OSError, TypeError):
            pass
        return False

    def signal_new_message(self) -> None:
        self._last_message_time = time.time()
        self._pending_encode = True

    async def retrieve_memories_hybrid(self, query: str, k: int = 5, use_reranker: bool = True) -> list[dict]:
        """FTS + 向量 RRF 混合检索 + Reranker 精排

        冷启动三段路由 (工业标准, 对标 Dify/Coze):
        - cold (0条):  纯 FTS, 向量检索完全关闭 → 零 Embedding 开销
        - warm (1~10条): FTS + 向量低权重融合, 向量仅做补充
        - hot  (>10条):  FTS + 向量均衡融合 (原有行为)

        Args:
            use_reranker: 是否在本方法内调用 Reranker 精排。A3 并行检索场景下会置为
                False，由调用方对合并后的候选池做一次性批量 Reranker。
        """
        _start = time.time()

        # ── 冷启动路由: 判断用户记忆档位 ──
        tier = await self.get_memory_tier()
        is_cold = tier == "cold"
        is_warm = tier == "warm"

        # 冷用户: 仅 FTS, 完全跳过向量检索 (零 Embedding 开销)
        # 但 FTS 无结果时仍尝试向量检索作为兜底（避免 cold_max > 0 时丢失向量召回）
        if is_cold:
            fts_items = await self._hybrid_fts_search(query, k)
            if fts_items:
                results = fts_items[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold", results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            # FTS 无结果，尝试向量兜底
            vec_items = await self._hybrid_vec_search(query, k)
            if vec_items:
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold+vec_fallback", results=len(vec_items),
                            duration_ms=int((time.time() - _start) * 1000))
                return vec_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier="cold", results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []

        # ── 温/热用户: 并行执行 FTS 与向量检索 ──
        # ContextNest A1: 提取确定性 selector → 候选集, 向量检索在候选集内排序
        selectors = self._extract_deterministic_selectors(query)
        candidate_ids = await self._get_candidate_ids_by_selectors(selectors, limit=k * 6)
        if candidate_ids is not None:
            logger.debug("memory.deterministic_selector",
                         selector_keys=[sk for sk in selectors if sk != "has_selectors"],
                         candidate_count=len(candidate_ids))
        fts_items, vec_items = await asyncio.gather(
            self._hybrid_fts_search(query, k),
            self._hybrid_vec_search(query, k, candidate_ids=candidate_ids),
        )

        # 空通道自动剔除: 两路都空则返回
        if not fts_items and not vec_items:
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []
        # 单路有结果: 直接返回
        if not fts_items:
            results = vec_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        if not vec_items:
            results = fts_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results

        # ── 加权 RRF 融合 ──
        try:
            import config as _cfg
            warm_vec_weight = getattr(_cfg, "MEMORY_WARM_VEC_WEIGHT", 0.2)
        except (ImportError, AttributeError):
            warm_vec_weight = 0.2
        # 温用户: 向量低权重 (default 0.2:0.8); 热用户: 均衡 (1.0:1.0)
        if is_warm:
            fts_weight, vec_weight = 1.0, warm_vec_weight
        else:
            fts_weight, vec_weight = 1.0, 1.0

        oversample_k = k * 3
        fts_ids = [str(item["id"]) for item in fts_items]
        vec_ids = [str(item["id"]) for item in vec_items]
        fused = reciprocal_rank_fusion(
            [fts_ids, vec_ids], limit=oversample_k,
            weights=[fts_weight, vec_weight],
        )

        # 按 RRF 排序获取完整记录
        all_items = {str(item["id"]): item for item in fts_items + vec_items}

        # Reranker 精排
        if use_reranker and self._reranker and self._reranker.available and len(fused) > k:
            reranked = await self._hybrid_rerank(query, fused, all_items, k)
            if reranked:
                results = reranked[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier=tier, results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results

        # 降级：无 Reranker 或 Reranker 失败时走 RRF 逻辑
        results = []
        for item_id, rrf_score in fused:
            if item_id in all_items:
                item = all_items[item_id]
                item["rrf_score"] = rrf_score
                results.append(item)

        final = results[:k]
        logger.info("memory.search", event="memory_search",
                    query=query[:100], tier=tier, results=len(final),
                    duration_ms=int((time.time() - _start) * 1000))
        return final

    async def _hybrid_fts_search(self, query: str, k: int) -> list[dict]:
        """FTS 检索"""
        if not self.memory:
            return []
        try:
            return await self.memory.search_memories_fts(query, limit=k * 2)
        except Exception as e:
            logger.warning("memory.fts_search_failed", error=str(e))
            return []

    async def _hybrid_vec_search(self, query: str, k: int,
                                 candidate_ids: list[int] | None = None) -> list[dict]:
        """向量检索 + 批量 JOIN：一次查询获取所有向量命中的记忆记录

        ContextNest A1: candidate_ids 提供时, 向量检索只在确定性候选集内排序,
        候选集本身由 metadata selector (时间/重要性) 产生, Jaccard 1.0。
        """
        if not self.vec:
            return []
        try:
            vec_results = await self.vec.search(
                query, top_k=k * 2, candidate_ids=candidate_ids, deterministic=True,
            )
            if not vec_results:
                return []
            vec_ids = [row_id for row_id, _ in vec_results]
            vec_mems = await self.memory.get_memories_by_ids(vec_ids)
            # 构建 id -> memory 映射，按 distance 排序组装结果
            vec_mem_map = {m["id"]: m for m in vec_mems}
            items = []
            if vec_results:
                if len(vec_results) == 1:
                    # 单条结果：min-max归一化退化(除以自身=0分)，直接用原始距离
                    _use_normalize = False
                else:
                    max_dist = max(d for _, d in vec_results)
                    _use_normalize = max_dist > 0
            for row_id, distance in vec_results:
                mem = vec_mem_map.get(row_id)
                if mem:
                    if _use_normalize:
                        mem["score"] = max(0.0, 1.0 - distance / max_dist)
                    else:
                        mem["score"] = max(0.0, 1.0 - distance)
                    items.append(mem)
            return items
        except Exception as e:
            logger.warning("memory.vec_search_failed", error=str(e))
            return []

    def _extract_deterministic_selectors(self, query: str) -> dict:
        """ContextNest A1: 从查询中提取确定性 selector (metadata-based, Jaccard 1.0)。

        与向量检索 (概率性, 论文实测 mean Jaccard 0.611) 互补:
        selector 先产生确定性候选集, 向量只在集内排序。

        Returns:
            dict 可选键:
            - time_range: (start_ts, end_ts) 来自"昨天/前天/上周"等时间词
            - min_importance: float  (当前留空, 由调用方按需填)
            - has_selectors: bool   是否有任何确定性 selector 可用
        """
        selectors: dict = {"has_selectors": False}
        try:
            tr = _parse_temporal_query(query)
            if tr:
                selectors["time_range"] = tr
                selectors["has_selectors"] = True
        except Exception as e:
            logger.debug("memory.selector_extract_failed", error=str(e))
        return selectors

    async def _get_candidate_ids_by_selectors(self, selectors: dict,
                                                limit: int = 200) -> list[int] | None:
        """根据确定性 selector 查询候选 rowid 集合。

        无 selector 返回 None (调用方走原 KNN 全量检索)。
        """
        if not selectors.get("has_selectors"):
            return None
        clauses: list[str] = []
        params: list = []
        if "time_range" in selectors:
            s, e = selectors["time_range"]
            clauses.append("timestamp BETWEEN ? AND ?")
            params.extend([s, e])
        if "min_importance" in selectors:
            clauses.append("importance >= ?")
            params.append(selectors["min_importance"])
        # ORDER BY id 保证候选集本身有序确定
        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        try:
            cursor = await self.memory._conn.execute(
                f"SELECT id FROM episodic_memories WHERE {where} "
                f"ORDER BY id LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows] if rows else []
        except Exception as e:
            logger.debug("memory.candidate_ids_failed", error=str(e))
            return None

    async def _hybrid_rerank(self, query: str, fused: list[tuple[str, float]],
                              all_items: dict[str, dict], k: int) -> list[dict] | None:
        """Reranker 精排：基于 RRF 融合后的候选池重排序，返回 top_k 结果。

        失败时返回 None，调用方降级到 RRF 排序。
        """
        docs: list[str] = []
        idx_map: dict[int, str] = {}
        for i, (item_id, _rrf_score) in enumerate(fused):
            if item_id in all_items:
                docs.append(all_items[item_id].get("summary", ""))
                idx_map[i] = item_id
        if not docs:
            return None
        try:
            reranked = await self._reranker.rerank(
                query=query,
                documents=docs,
                top_n=k,
            )
            results: list[dict] = []
            for item in reranked:
                orig_idx = item["index"]
                item_id = idx_map.get(orig_idx)
                if item_id and item_id in all_items:
                    mem = all_items[item_id]
                    mem["rerank_score"] = item["relevance_score"]
                    mem["rrf_score"] = dict(fused).get(item_id, 0)
                    results.append(mem)
            return results if results else None
        except Exception as e:
            logger.warning("memory.rerank_failed", error=str(e))
            return None

    def _is_retrieval_simple(self, query: str) -> bool:
        """A1: 判断查询是否足够简单，可跳过查询变换直接走混合检索

        判定规则（按顺序短路）:
        1. 计算有效长度（中文字符 ×2 + 其他字符 ×1），<=15 直接判定为简单
        2. 命中 SIMPLE_TASK_KEYWORDS["chat"] → 简单
        3. 命中 SIMPLE_TASK_KEYWORDS["complex"] → 非简单
        4. 有效长度 <=20 且无复杂关键词 → 简单
        5. 否则 → 非简单
        """
        if not query:
            return True

        # 计算有效长度：中文字符 ×2 + 其他字符 ×1
        effective_len = 0
        for ch in query:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                effective_len += 2
            else:
                effective_len += 1

        # 规则 1：极短查询直接跳过变换
        if effective_len <= 15:
            return True

        # 规则 2 & 3：关键词匹配
        try:
            from config import SIMPLE_TASK_KEYWORDS
            chat_keywords = SIMPLE_TASK_KEYWORDS.get("chat", [])
            complex_keywords = SIMPLE_TASK_KEYWORDS.get("complex", [])
        except (ImportError, AttributeError):
            chat_keywords = []
            complex_keywords = []

        for kw in chat_keywords:
            if kw in query:
                return True

        for kw in complex_keywords:
            if kw in query:
                return False

        # 规则 4：中等长度且无复杂关键词
        if effective_len <= 20:
            return True

        # 规则 5
        return False

    def _suggest_k(self, query: str, default_k: int = 3) -> int:
        """根据查询内容智能建议检索条数 k（情感陪伴型 bot）。

        策略：
        - 极短闲聊（问候/确认）：k=1，避免注入无关记忆
        - 日常闲聊：k=2~3
        - 情感/回忆/个人话题：k=4~5，多检索相关情感记忆
        - 涉及具体事件/人物/经历：k=4，召回更多上下文
        """
        if not query:
            return 1

        # 计算有效长度
        effective_len = 0
        for ch in query:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                effective_len += 2
            else:
                effective_len += 1

        # 极短查询：问候、确认、单字回复
        if effective_len <= 8:
            return 1

        # 短查询：简单闲聊
        if effective_len <= 15:
            return 2

        # 情感/回忆/个人话题 → 多检索，让回复更有温度和连贯性
        emotional_indicators = (
            "记得", "想起", "回忆", "以前", "之前", "那时候", "那次",
            "喜欢", "讨厌", "开心", "难过", "伤心", "生气", "害怕",
            "担心", "焦虑", "压力", "累", "烦", "无聊", "孤独",
            "想你", "想ta", "分手", "吵架", "和好", "朋友", "家人",
            "爸妈", "生日", "节日", "考试", "面试", "工作", "辞职",
            "梦想", "未来", "以后", "遗憾", "后悔", "感恩", "幸福",
            "害怕", "勇敢", "加油", "坚持", "放弃", "努力",
            "心情", "感觉", "感受", "情绪", "状态", "最近",
        )
        query_lower = query.lower()
        for indicator in emotional_indicators:
            if indicator in query_lower:
                return min(5, default_k + 2)

        # 涉及具体事件/人物/经历
        event_indicators = (
            "发生", "那次", "那件事", "什么时候", "哪里", "谁",
            "聊天", "说过", "告诉你", "跟我说", "你记得",
            "上次", "上次说", "之前说", "你说过",
        )
        for indicator in event_indicators:
            if indicator in query_lower:
                return min(4, default_k + 1)

        # 长查询：可能涉及多话题
        if effective_len > 60:
            return min(5, default_k + 2)

        return default_k

    async def retrieve_memories(self, query: str, k: int = 5, context: str = "") -> list[dict]:
        import config
        # 时间实体识别：检测"昨天/前天/上周"等时间词，按时间范围检索
        # 这让小妲能回答"昨天发生了什么"这类纯时间查询
        temporal_results = await self._try_temporal_search(query, k)
        if temporal_results is not None:
            return temporal_results

        # A1: 智能短路 - 简单查询跳过查询变换，直接走混合检索
        if getattr(config, "RETRIEVAL_SMART_SKIP", True) and self._is_retrieval_simple(query):
            results = await self.retrieve_memories_hybrid(query, k=k)
            if results:
                # 简单路径使用与复杂路径一致的评分逻辑，保证评分尺度统一
                results = await self._apply_fluid_scoring(results)
                query_entities: set[str] = set()
                if self.kg:
                    try:
                        query_entities = await self.kg.get_query_entities(query)
                    except Exception:
                        pass
                await self._compute_final_scores(query, results, config, query_entities)
                results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
                results = results[:k]
            return results

        # 查询变换：改写 + 扩展
        queries = await self._transform_queries(query, context)

        # 多查询检索
        if getattr(config, "RETRIEVAL_PARALLEL_SEARCH", True) and len(queries) > 1:
            all_results = await self._multi_query_parallel_search(queries, query, k)
        else:
            all_results = await self._multi_query_serial_search(queries, k)
        results = all_results

        # 降级：纯向量检索
        if not results:
            results = await self._vector_fallback_search(query, k)

        # 最终兜底：重要性排序
        if not results:
            results = await self._importance_fallback_search(k)

        # 流体记忆评分（艾宾浩斯遗忘曲线 + 访问强化）
        results = await self._apply_fluid_scoring(results)

        # I6: KG 召回通道 — query 实体 → KG 关联实体 → 反查记忆加入候选池
        #     让 KG 真正参与召回（原实现仅在评分阶段后置增强）
        query_entities: set[str] = set()
        if self.kg:
            try:
                query_entities = await self.kg.get_query_entities(query)
                if query_entities:
                    related_names = await self.kg.recall_by_entities(
                        query_entities, limit=5)
                    if related_names:
                        kg_hits = await self.memory.search_memories_by_entities(
                            related_names, limit=k)
                        _existing_ids = {str(r.get("id", "")) for r in results}
                        _added = 0
                        for _m in kg_hits:
                            _mid = str(_m.get("id", ""))
                            if _mid and _mid not in _existing_ids:
                                _existing_ids.add(_mid)
                                # KG 召回的记忆没有 rerank/rrf 分数，给默认值参与综合评分
                                _m.setdefault("effective_score",
                                              _m.get("importance", 0.5) * 0.6)
                                _m["kg_recall"] = True
                                results.append(_m)
                                _added += 1
                        if _added:
                            logger.debug("memory.kg_recall",
                                         entities=len(related_names), added=_added)
            except Exception as e:
                logger.debug("memory.kg_recall_failed", error=str(e))

        # KG 增强评分 + 综合评分 (复用已提取的 query_entities, 避免 N+1 LLM)
        await self._compute_final_scores(query, results, config, query_entities)

        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        results = results[:k]

        # 主动检索 A：话题触发器
        # 从 query 抽取 top-N 话题关键词，对每个词做轻量 FTS 检索，
        # 把"主题相关但未被主路命中"的记忆补充进来，扩大主动联想。
        # 这样即使主路 RRF 没召回，话题相关的旧记忆也能浮上来。
        results = await self._apply_topic_trigger(query, results, k)

        # KG 上下文增强（保留原有逻辑）
        await self._apply_kg_context_enhance(results)

        return results

    async def _try_temporal_search(self, query: str, k: int) -> list[dict] | None:
        """时间实体识别：检测"昨天/前天/上周"等时间词，按时间范围检索。

        无时间词返回 None（调用方继续走常规检索）；命中则返回结果列表。
        Q1-2: 时间+内容混合查询应用 reranker 精排，避免返回时间范围内不相关的记忆。
        """
        _time_range = _parse_temporal_query(query)
        if not _time_range:
            return None
        start_ts, end_ts = _time_range
        try:
            # 优先尝试 FTS + 时间过滤（多检索一些，给 reranker 精排空间）
            _fts_results = await self.memory.search_memories_fts_with_time(
                query, start_ts, end_ts, limit=k * 2
            )
            if _fts_results:
                _fts_results = await self._apply_reranker_to_results(query, _fts_results, k)
                logger.debug("memory.temporal_fts_hit",
                             query=query[:50], count=len(_fts_results))
                return _fts_results
            # FTS 无结果，退回纯时间检索
            _time_results = await self.memory.search_memories_by_time(
                start_ts, end_ts, limit=k * 2
            )
            if _time_results:
                _time_results = await self._apply_reranker_to_results(query, _time_results, k)
                logger.debug("memory.temporal_time_hit",
                             query=query[:50], count=len(_time_results))
            return _time_results
        except Exception as e:
            logger.warning("memory.temporal_search_failed", error=str(e))
            return None

    async def _apply_reranker_to_results(self, query: str, results: list[dict],
                                          k: int) -> list[dict]:
        """对检索结果应用 reranker 精排（如果可用且结果数 > k）。

        失败时返回原结果前 k 条（不降级到空）。
        """
        if not results or not self._reranker or not self._reranker.available:
            return results[:k]
        if len(results) <= k:
            return results  # 结果数不足，无需精排
        try:
            docs = [r.get("summary", "") for r in results]
            reranked = await self._reranker.rerank(query=query, documents=docs, top_n=k)
            if not reranked:  # Q0-2: reranker 失败返回空列表，降级到原顺序
                return results[:k]
            reordered = []
            for item in reranked:
                idx = item.get("index", 0)
                if 0 <= idx < len(results):
                    mem = results[idx]
                    mem["rerank_score"] = item.get("relevance_score", 0.0)
                    reordered.append(mem)
            return reordered if reordered else results[:k]
        except Exception as e:
            logger.debug("memory.rerank_apply_failed", error=str(e))
            return results[:k]

    async def _transform_queries(self, query: str, context: str) -> list[str]:
        """查询变换：rewrite + expand。A2 并行执行，失败降级到 [query]。"""
        import config
        queries = [query]
        if not (self._query_transformer and getattr(config, "QUERY_TRANSFORM_ENABLED", True)):
            return queries
        parallel_transform = getattr(config, "RETRIEVAL_PARALLEL_TRANSFORM", True)
        try:
            if parallel_transform:
                # A2: 并行执行 rewrite + expand（各自独立的 LLM 调用）
                expand_count = getattr(config, "QUERY_EXPAND_COUNT", 2)
                rewrite_task = asyncio.create_task(
                    self._query_transformer.rewrite_query(query, context)
                )
                expand_task = asyncio.create_task(
                    self._query_transformer.expand_query(query, n=expand_count)
                )
                rewritten, expanded = await asyncio.gather(
                    rewrite_task, expand_task, return_exceptions=True
                )
                # 异常降级：rewrite 失败用原查询，expand 失败用 [query]
                if isinstance(rewritten, Exception):
                    logger.warning("memory.rewrite_failed", error=str(rewritten))
                    rewritten = query
                if isinstance(expanded, Exception):
                    logger.warning("memory.expand_failed", error=str(expanded))
                    expanded = [query]
                if not rewritten:
                    rewritten = query
                if not expanded:
                    expanded = [query]
                if rewritten != query:
                    logger.debug("memory.query_rewritten",
                                 original=query[:50], rewritten=rewritten[:50])
                # 合并：[rewritten] + [q for q in expanded if q != rewritten]
                merged = [rewritten]
                for q in expanded:
                    if q != rewritten:
                        merged.append(q)
                queries = merged
                if len(queries) > 1:
                    logger.debug("memory.query_expanded", count=len(queries))
            else:
                # 串行降级（原有逻辑）
                rewritten = await self._query_transformer.rewrite_query(query, context)
                if rewritten and rewritten != query:
                    queries = [rewritten]
                    logger.debug("memory.query_rewritten", original=query[:50], rewritten=rewritten[:50])
                expand_count = getattr(config, "QUERY_EXPAND_COUNT", 2)
                if expand_count > 0:
                    expanded = await self._query_transformer.expand_query(rewritten, n=expand_count)
                    if expanded and len(expanded) > 1:
                        queries = expanded
                        logger.debug("memory.query_expanded", count=len(queries))
        except Exception as e:
            logger.warning("memory.query_transform_failed", error=str(e))
        return queries

    async def _multi_query_parallel_search(self, queries: list[str], query: str,
                                             k: int) -> list[dict]:
        """A3: 并行多查询检索 + 批量 Reranker。

        各子查询检索时关闭内部 Reranker，统一在合并池上做一次批量精排。
        """
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        hybrid_tasks = [
            self.retrieve_memories_hybrid(q, k=k * 2, use_reranker=False)
            for q in queries
        ]
        hybrid_results = await asyncio.gather(*hybrid_tasks, return_exceptions=True)
        for i, res in enumerate(hybrid_results):
            if isinstance(res, Exception):
                logger.warning("memory.hybrid_search_failed",
                               query=queries[i][:50], error=str(res))
                continue
            for r in res:
                rid = str(r.get("id", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    all_results.append(r)
        # 批量 Reranker：对合并后的候选池用原始 query 重排一次
        if self._reranker and self._reranker.available and len(all_results) > k:
            try:
                docs = [r.get("summary", "") for r in all_results]
                reranked = await self._reranker.rerank(
                    query=query,
                    documents=docs,
                    top_n=k,
                )
                reranked_results = []
                for item in reranked:
                    idx = item.get("index", -1)
                    if 0 <= idx < len(all_results):
                        mem = all_results[idx]
                        mem["rerank_score"] = item.get("relevance_score", 0.0)
                        reranked_results.append(mem)
                if reranked_results:
                    all_results = reranked_results
            except Exception as e:
                logger.warning("memory.batch_rerank_failed", error=str(e))
        return all_results

    async def _multi_query_serial_search(self, queries: list[str], k: int) -> list[dict]:
        """串行降级（原有逻辑）。"""
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        for q in queries:
            try:
                hybrid_results = await self.retrieve_memories_hybrid(q, k=k)
                for r in hybrid_results:
                    rid = str(r.get("id", ""))
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        all_results.append(r)
            except Exception as e:
                logger.warning("memory.hybrid_search_failed", query=q[:50], error=str(e))
        return all_results

    async def _vector_fallback_search(self, query: str, k: int) -> list[dict]:
        """降级：纯向量检索 + 批量 JOIN。"""
        if not self.vec:
            return []
        results: list[dict] = []
        try:
            vec_results = await self.vec.search(query, top_k=k)
            if vec_results:
                vec_ids = [row_id for row_id, _ in vec_results]
                vec_mems = await self.memory.get_memories_by_ids(vec_ids)
                # 构建 id -> memory 映射，按 distance 排序组装结果
                vec_mem_map = {m["id"]: m for m in vec_mems}
                for row_id, distance in vec_results:
                    mem = vec_mem_map.get(row_id)
                    if mem:
                        mem["score"] = 1.0 - distance
                        results.append(mem)
        except Exception as e:
            logger.warning("memory.vec_search_failed", error=str(e))
        return results

    async def _importance_fallback_search(self, k: int) -> list[dict]:
        """最终兜底：按重要性排序检索。"""
        if not self.memory:
            return []
        try:
            return await self.memory.search_memories_by_importance(
                min_importance=0.4, limit=k
            )
        except Exception as e:
            logger.warning("memory.fallback_search_failed", error=str(e))
            return []

    async def _apply_fluid_scoring(self, results: list[dict]) -> list[dict]:
        """流体记忆评分（艾宾浩斯遗忘曲线 + 访问强化），过滤低分记忆。

        对保留的记忆递增 access_count 实现检索强化。
        """
        if not results:
            return results
        _fluid = FluidMemory()
        filtered: list[dict] = []
        for r in results:
            created_at = r.get("timestamp", time.time())
            access_count = r.get("access_count", 0)
            similarity = r.get("score", 0.5)
            fluid_score = _fluid.score(similarity, created_at, access_count)
            if _fluid.should_filter(fluid_score):
                continue
            importance = r.get("importance", 0.5)
            r["effective_score"] = importance * fluid_score
            filtered.append(r)
        if filtered:
            for r in filtered:
                await self.memory.increment_access_count(r["id"], auto_commit=False)
            try:
                await self.memory.commit()
            except Exception as e:
                logger.debug("memory.fluid_access_count_commit_failed", error=str(e))
        return filtered

    async def _compute_final_scores(self, query: str, results: list[dict],
                                      config: Any,
                                      query_entities: set[str] | None = None) -> None:
        """KG 增强评分 + 综合评分: final = α×rerank + β×kg_boost + γ×(importance×decay)。

        I6: 复用已存储的 entities 字段 + 预提取的 query_entities，
        避免 N+1 次 LLM 调用（原 get_relevance_boost 性能黑洞）。
        """
        if not results:
            return
        kg_boosts: list[float] = [0.0] * len(results)
        if self.kg:
            try:
                import json
                if query_entities is None:
                    query_entities = await self.kg.get_query_entities(query)
                if query_entities:
                    memory_entities_list: list[list[str]] = []
                    for r in results:
                        raw = r.get("entity_list") or r.get("entities", [])
                        if isinstance(raw, str) and raw:
                            try:
                                raw = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                raw = []
                        memory_entities_list.append(
                            raw if isinstance(raw, list) else [])
                    kg_boosts = await self.kg.get_relevance_boost_fast(
                        query_entities, memory_entities_list)
            except Exception as e:
                logger.debug("memory.kg_boost_failed", error=str(e))
        # 综合评分
        alpha = getattr(config, "RAG_RERANK_WEIGHT", 0.65)
        beta = getattr(config, "RAG_KG_WEIGHT", 0.15)
        gamma = getattr(config, "RAG_IMPORTANCE_WEIGHT", 0.20)
        for i, r in enumerate(results):
            rerank_score = r.get("rerank_score", r.get("rrf_score", r.get("effective_score", 0.5)))
            kg_boost = kg_boosts[i] if i < len(kg_boosts) else 0.0
            importance_decay = r.get("effective_score", 0.5)
            r["final_score"] = alpha * rerank_score + beta * kg_boost + gamma * importance_decay

    async def _apply_topic_trigger(self, query: str, results: list[dict],
                                     k: int) -> list[dict]:
        """主动检索 A：话题触发器。

        从 query 抽取 top-N 话题关键词，对每个词做轻量 FTS 检索，
        把"主题相关但未被主路命中"的记忆补充进来，扩大主动联想。
        即使主路 RRF 没召回，话题相关的旧记忆也能浮上来。
        """
        try:
            _topic_keywords = _extract_topic_keywords(query, top_n=2)
            if not _topic_keywords:
                return results
            _existing_ids = {str(r.get("id", "")) for r in results}
            for _kw in _topic_keywords:
                # 跳过和原 query 完全相同的关键词（已被主路检索过）
                if _kw == query or _kw in query:
                    continue
                _topic_hits = await self.memory.search_memories_fts(_kw, limit=1)
                for _r in _topic_hits:
                    _rid = str(_r.get("id", ""))
                    if _rid and _rid not in _existing_ids:
                        _existing_ids.add(_rid)
                        # 标记话题触发来源，便于调试和上层 prompt 区分
                        _r["topic_trigger"] = _kw
                        # 话题触发的记忆没有 final_score，用基础分填充避免排序异常
                        _r.setdefault("final_score", _r.get("score", 0.3) * 0.5)
                        results.append(_r)
            if len(results) > k:
                results = results[:k]
            logger.debug("memory.topic_trigger",
                         keywords=_topic_keywords,
                         added=sum(1 for r in results if r.get("topic_trigger")))
        except Exception as e:
            logger.debug("memory.topic_trigger_failed", error=str(e))
        return results

    async def _apply_kg_context_enhance(self, results: list[dict]) -> None:
        """KG 上下文增强：对 top-2 记忆提取实体并补充相关知识点。"""
        if not (self.kg and results):
            return
        try:
            entity_names: list[str] = []
            for r in results[:2]:
                summary = r.get("summary", "")
                candidates = _extract_entities(summary)
                for word in candidates:
                    if word not in ("用户", "助手", "人家"):
                        entity_names.append(word)
            entity_names = list(set(entity_names))[:3]
            if entity_names:
                knowledge = await self.kg.get_related_knowledge(entity_names)
                if knowledge:
                    kg_context = await self.kg.format_knowledge_context(knowledge)
                    if kg_context and results:
                        results[0]["kg_context"] = kg_context
        except Exception as e:
            logger.debug("memory.kg_expand_failed", error=str(e))
    async def encode_memory(self, context: dict) -> None:
        exchanges = context.get("exchanges", [])
        if not exchanges or len(exchanges) < 2:
            return

        summary = self._generate_summary(exchanges)

        # 安全过滤
        validation = validate_memory_content(summary)
        if validation:
            logger.warning("memory.safety_blocked", reason=validation)
            return

        # 去重检查
        if await self._has_duplicate(summary):
            logger.debug("memory.duplicate_skipped", summary=summary[:80])
            return

        # 原有安全扫描（保留兼容）
        from security.security import SecurityFilter
        security = self._security_filter or SecurityFilter()
        threat_result = security.scan_threats(summary, scope="strict")
        if not threat_result.is_safe and threat_result.action == "block":
            logger.warning("memory.security_blocked", threat=threat_result.threat_type)
            return

        importance = self._estimate_importance(exchanges, context)
        emotion = context.get("emotion", {}).get("primary", "")

        # 规则提取增强重要性
        user_msg = ""
        assistant_msg = ""
        for msg in exchanges[-6:]:
            if msg.get("role") == "user":
                user_msg += msg.get("content", "") + " "
            elif msg.get("role") == "assistant":
                assistant_msg += msg.get("content", "") + " "
        rule_extractor = RuleBasedMemoryExtractor()
        rule_matches = rule_extractor.extract(user_msg, assistant_msg)
        if rule_matches:
            best_rule = max(rule_matches, key=lambda r: r["importance"])
            importance = max(importance, best_rule["importance"])

        try:
            # 写入候选审计表
            candidate_id = await self.memory.insert_consolidation_candidate(
                source="encode",
                kind=rule_matches[0]["kind"] if rule_matches else "episodic",
                summary=summary,
                confidence=rule_matches[0]["confidence"] if rule_matches else 0.5,
                importance=importance,
            )

            mem_id = await self.memory.insert_episodic_memory(
                summary=summary,
                importance=importance,
                emotion_label=emotion,
            )

            # 标记候选已应用
            await self.memory.mark_candidate_applied(candidate_id, mem_id)

            # ContextNest A3: 记录初始版本哈希链 (tamper-evident)
            if self._governance:
                try:
                    await self._governance.record_initial_version(mem_id, summary, auto_commit=False)
                except Exception as e:
                    logger.debug("memory.governance_init_failed", error=str(e))

            if self.vec and summary:
                try:
                    await self.vec.upsert(mem_id, summary)
                except Exception as e:
                    logger.debug("memory.initial_vec_upsert_failed", error=str(e))

            self._last_encode_time = time.time()
            self._pending_encode = False
            logger.info("memory.encoded", summary=summary[:80], importance=importance)

            # 冷启动路由: 新记忆写入后失效计数缓存, 下次检索立即感知档位变化
            self.invalidate_memory_count_cache()

            self._save_state_json(summary, importance, emotion)

            # fire-and-forget 后台 LLM 结构化提取（不阻塞主流程）
            # 用 GLM-4-9B-0414 提取实体/事件/决策/偏好，完成后更新记忆条目
            try:
                _enrich_task = asyncio.create_task(
                    self._enrich_memory_async(mem_id, exchanges)
                )
                def _log_enrich_exception(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.warning("memory.enrich_async_failed", error=str(exc))

                _enrich_task.add_done_callback(_log_enrich_exception)
            except Exception as e:
                logger.debug("memory.enrich_spawn_failed", error=str(e))
        except Exception as e:
            logger.warning("memory.encode_failed", error=str(e))

        if self.kg and summary:
            try:
                await self.kg.auto_extract_and_merge(summary)
            except Exception as e:
                logger.debug("memory.kg_extract_failed", error=str(e))

    def _generate_summary(self, exchanges: list[dict]) -> str:
        parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                parts.append(f"用户说: {content[:150]}")
            elif role == "assistant" and content:
                parts.append(content[:150])

        summary = "；".join(parts)
        return summary[:500]

    async def _enrich_memory_async(self, mem_id: int, exchanges: list[dict]) -> None:
        """后台 LLM 提取：用 GLM-4-9B-0414 从对话中提取结构化信息，更新记忆条目。

        fire-and-forget 调用，不阻塞主流程。失败静默（记忆保留原始字符串摘要）。
        提取内容：更高质量摘要、实体列表、事件类型、元数据（决策/话题/情绪）。
        """
        import json
        try:
            # 构建对话文本（比 _generate_summary 保留更多内容，给 LLM 更多上下文）
            lines = []
            for msg in exchanges[-6:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and content:
                    lines.append(f"用户: {content[:150]}")
                elif role == "assistant" and content:
                    lines.append(f"{get_agent_display_name('xiaoda')}: {content[:150]}")
            text = "\n".join(lines)
            if not text or len(text) < 10:
                return

            prompt = f"""你是记忆结构化提取助手。从以下对话中提取结构化信息，返回 JSON 格式（只返回 JSON，不要任何其他内容）：

对话内容：
{text}

请返回以下 JSON 格式：
{{
  "summary": "更高质量的摘要，保留关键信息、决策、偏好，80字以内",
  "entities": ["涉及的人物、物品、地点、技术名词等实体"],
  "event_type": "事件类型（对话/决策/偏好/事件/闲聊/调试/学习 之一）",
  "metadata": {{
    "decision": "如果有决策或结论写在这里，没有则空字符串",
    "topic": "主要话题，1-3个词",
    "mood": "用户情绪（喜悦/悲伤/愤怒/平静/焦虑等）"
  }}
}}"""

            messages = [{"role": "user", "content": prompt}]
            result = await self.distiller._call_free_model(messages, temperature=0.3, max_tokens=400)
            if not result:
                return

            # 去除可能的 <think> 标签
            if "<think>" in result:
                result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()

            # 提取 JSON（LLM 可能返回带 markdown 代码块的）
            json_str = result
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
            json_str = json_str.strip()

            data = json.loads(json_str)

            new_summary = data.get("summary", "").strip()
            entities = json.dumps(data.get("entities", []), ensure_ascii=False)
            event_type = data.get("event_type", "").strip()
            metadata = json.dumps(data.get("metadata", {}), ensure_ascii=False)

            # 更新 DB（summary 只在长度足够时才更新，避免丢失信息）
            update_summary = new_summary if new_summary and len(new_summary) >= 20 else ""
            await self.memory.update_memory_enrichment(
                mem_id,
                summary=update_summary,
                entities=entities,
                event_type=event_type,
                metadata_json=metadata,
            )

            # ContextNest A3: summary 变更时记录新版本到哈希链
            if self._governance and update_summary:
                try:
                    await self._governance.record_version_update(mem_id, update_summary)
                except Exception as e:
                    logger.debug("memory.governance_update_failed", error=str(e))

            # 如果 summary 更新了，重新生成向量（让向量检索也能用到更好的摘要）
            if update_summary and self.vec:
                try:
                    await self.vec.upsert(mem_id, update_summary)
                except Exception as e:
                    logger.debug("memory.enrich_vec_failed", error=str(e))

            logger.info("memory.enriched", mem_id=mem_id, event_type=event_type,
                        entities_count=len(data.get("entities", [])))
        except Exception as e:
            logger.debug("memory.enrich_failed", error=str(e))

    def _estimate_importance(self, exchanges: list[dict], context: dict) -> float:
        importance = 0.3

        emotion = context.get("emotion", {})
        if emotion.get("primary") in ("悲伤", "愤怒", "焦虑", "恐惧"):
            importance += 0.3
        elif emotion.get("primary") in ("喜悦", "感激", "期待"):
            importance += 0.1

        total_len = sum(len(m.get("content", "")) for m in exchanges)
        if total_len > 500:
            importance += 0.2

        return min(importance, 1.0)

    async def try_idle_encode(self, context: dict, force: bool = False) -> None:
        now = time.time()
        if not self._pending_encode:
            return
        if not force and now - self._last_message_time < self.IDLE_THRESHOLD:
            return
        if now - self._last_encode_time < self.ENCODE_COOLDOWN:
            return

        await self.encode_memory(context)

    def _save_state_json(self, summary: str, importance: float, emotion: str) -> None:
        """原子写入记忆状态到 JSON 文件"""
        try:
            from pathlib import Path
            # 使用用户数据目录，避免写入 _MEIPASS 只读目录
            try:
                from config import MEMORY_STATE_DIR
                state_dir = MEMORY_STATE_DIR
            except ImportError:
                # 避免在 PyInstaller frozen 模式下写入 _MEIPASS 只读目录
                state_dir = Path.home() / ".ai-agent" / "memory_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_path = str(state_dir / "memory_state.json")
            data = {
                "last_summary": summary[:500],
                "last_importance": importance,
                "last_emotion": emotion,
                "last_encode_time": self._last_encode_time,
            }
            atomic_json_write(state_path, data)
        except Exception as e:
            logger.warning("memory.state_json_save_failed", error=str(e))

    async def distill_old_memories(self) -> int:
        """P3: 蒸馏超过阈值的旧记忆为摘要。

        查询未蒸馏记忆数量，若超过 MAX_EPISODIC_MEMORIES 阈值，
        取最旧的 MEMORY_DISTILL_BATCH 条蒸馏为摘要，并标记为 distilled=1。

        Returns:
            本次蒸馏的记忆条数（0 表示未触发或无候选）
        """
        import config
        max_memories = getattr(config, "MAX_EPISODIC_MEMORIES", 200)
        batch = getattr(config, "MEMORY_DISTILL_BATCH", 30)

        try:
            count = await self.memory.get_episodic_count_undistilled()
            if count <= max_memories:
                return 0

            candidates = await self.memory.get_distill_candidates(limit=batch)
            if not candidates:
                return 0

            summary = await self.distiller.distill(candidates)
            if not summary:
                logger.warning("memory.distill_empty_summary", candidates=len(candidates))
                return 0

            # 写入摘要表 + 标记原记忆为已蒸馏（同一事务，避免重复蒸馏）
            memory_ids = [c["id"] for c in candidates if c.get("id") is not None]
            await self.memory.insert_memory_summary(
                summary_text=summary, memory_count=len(candidates), auto_commit=False,
            )
            await self.memory.mark_memories_distilled(memory_ids, auto_commit=False)
            await self.memory.commit()

            logger.info("memory.distilled",
                        count=len(candidates),
                        undistilled_before=count,
                        summary_len=len(summary))
            return len(candidates)
        except Exception as e:
            logger.warning("memory.distill_failed", error=str(e))
            return 0

    async def run_scheduled_recall(self, *, hours_back: float = 3.0,
                                    min_importance: float = 0.6,
                                    min_memories: int = 3) -> int:
        """主动检索 B：定时回忆任务。

        从 hours_back 小时前到现在，取重要性 >= min_importance 的记忆，
        若数量 >= min_memories，调用 distill_recall 整理成"回忆笔记"，
        写入 memory_recall_notes 表。后续 retrieve_memories/build_memory_prompt
        可主动拉取这些笔记作为高密度上下文。

        Args:
            hours_back: 回顾窗口的小时数（默认 3h）
            min_importance: 重要性下限（默认 0.6）
            min_memories: 触发整理的最小记忆条数（少于则跳过本次）

        Returns:
            本次整理的源记忆条数（0 表示未触发或无候选）
        """
        try:
            now = time.time()
            window_start = now - hours_back * 3600.0
            candidates = await self.memory.get_high_importance_since(
                start_ts=window_start,
                min_importance=min_importance,
                limit=50,
            )
            if len(candidates) < min_memories:
                logger.debug("memory.recall_skipped",
                             reason="insufficient_memories",
                             count=len(candidates),
                             min=min_memories)
                return 0

            # 调用叙事风格蒸馏
            note = await self.distiller.distill_recall(candidates)
            if not note:
                logger.warning("memory.recall_empty_note", candidates=len(candidates))
                return 0

            # 从候选中提取标签（前 5 个实体的并集，便于日后按标签检索）
            tags_set: list[str] = []
            seen = set()
            for c in candidates[:10]:
                ents = (c.get("entities") or "").strip()
                if ents:
                    for e in ents.split("|"):
                        e = e.strip()
                        if e and e not in seen and len(e) >= 2:
                            seen.add(e)
                            tags_set.append(e)
                        if len(tags_set) >= 5:
                            break
                if len(tags_set) >= 5:
                    break
            tags = "|".join(tags_set)

            # 用第一条记忆的时间戳作为 window_start 的实际值（更精确）
            try:
                real_start = min(float(c.get("timestamp", now)) for c in candidates)
            except (ValueError, TypeError):
                real_start = window_start

            source_ids = ",".join(str(c.get("id", "")) for c in candidates if c.get("id"))

            note_id = await self.memory.insert_recall_note(
                window_start=real_start,
                window_end=now,
                summary=note,
                memory_count=len(candidates),
                min_importance=min_importance,
                source_memory_ids=source_ids,
                title=f"回忆笔记 {time.strftime('%m-%d %H:%M', time.localtime(real_start))}~{time.strftime('%H:%M', time.localtime(now))}",
                tags=tags,
            )

            logger.info("memory.recall_note_created",
                        note_id=note_id,
                        source_count=len(candidates),
                        window_hours=hours_back,
                        note_len=len(note))
            return len(candidates)
        except Exception as e:
            logger.warning("memory.run_scheduled_recall_failed", error=str(e))
            return 0

    async def retrieve_comfort_memories(self, limit: int = 2) -> list[dict]:
        """主动检索 C：情绪触发 — 检索"安抚性记忆"。

        当检测到用户情绪低落（valence=negative）时，主动检索带正面情绪标签
        的历史记忆（喜悦/happy），作为"安抚素材"注入上下文，让小妲能
        回忆起"曾经让用户开心的事"来温柔陪伴。

        DB 中 emotion_label 列历史数据是中文（喜悦），统一模式后是英文（happy），
        所以两种标签都查，避免漏检。

        Args:
            limit: 返回条数上限（默认 2，避免上下文膨胀）

        Returns:
            安抚性记忆列表，每条带 emotion_trigger="comfort" 标记
        """
        try:
            # 正面情绪标签：中文 + 英文双查
            # 喜悦 = happy；害羞有时也带正面色彩（用户被逗笑），但保守起见只取喜悦
            comfort_labels = ["喜悦", "happy"]
            results = await self.memory.search_memories_by_emotion(
                comfort_labels, limit=limit
            )
            for r in results:
                # 标记来源，便于 prompt 层区分和调试
                r["emotion_trigger"] = "comfort"
            if results:
                logger.debug("memory.comfort_memories_retrieved",
                             count=len(results),
                             labels=comfort_labels)
            return results
        except Exception as e:
            logger.warning("memory.retrieve_comfort_memories_failed", error=str(e))
            return []

    async def build_memory_prompt(self, recent_limit: int = 20,
                                   summary_limit: int = 5,
                                   include_recall_note: bool = True) -> str:
        """P3: 构建记忆提示文本，优先使用蒸馏摘要 + 近期未蒸馏记忆。

        Args:
            recent_limit: 近期未蒸馏记忆条数上限
            summary_limit: 蒸馏摘要条数上限
            include_recall_note: 是否在提示开头注入最近一条定时回忆笔记

        Returns:
            记忆提示文本，无内容时返回空串。
        """
        try:
            summaries = await self.memory.get_memory_summaries(limit=summary_limit)
            recent = await self.memory.get_recent_undistilled(limit=recent_limit)
            recall_notes = []
            if include_recall_note:
                # 只取最近 1 条回忆笔记（避免上下文膨胀）
                recall_notes = await self.memory.get_recent_recall_notes(limit=1)
        except Exception as e:
            logger.debug("memory.build_prompt_failed", error=str(e))
            return ""

        if not summaries and not recent and not recall_notes:
            return ""

        parts = []
        # 定时回忆笔记放在最前（最高密度上下文，像"刚才发生了什么"的快照）
        if recall_notes:
            parts.append("[最近回忆笔记]")
            for rn in recall_notes:
                text = (rn.get("summary") or "").strip()
                if text:
                    parts.append(f"· {text}")

        if summaries:
            parts.append("[历史记忆摘要]")
            for s in summaries:
                text = (s.get("summary_text") or "").strip()
                if text:
                    parts.append(f"· {text}")

        if recent:
            if parts:
                parts.append("[近期记忆]")
            else:
                parts.append("[记忆]")
            for r in reversed(recent):  # 按时间升序展示
                text = (r.get("summary") or "").strip()
                if text:
                    parts.append(f"· {text}")

        return "\n".join(parts)

    async def shutdown(self) -> str:
        if self.vec:
            await self.vec.close()
        return "done"