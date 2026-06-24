import asyncio
import re
import time
from loguru import logger

from db.database import DatabaseManager
from db.db_memory import MemoryDB
from .vector_store import VectorStore
from .fluid_memory import FluidMemory
from utils.atomic_write import atomic_json_write


def _extract_entities(text: str) -> list[str]:
    try:
        import jieba
        words = jieba.cut(text)
        return [w for w in words if len(w) >= 2]
    except ImportError:
        return [text[i:i+n] for n in range(2, 5) for i in range(len(text)-n+1)]


# ── FTS5 预分词工具 ──
_CJK_RANGE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
_KEYWORD_SPLIT = re.compile(r'[^\w]+')
_FTS_SPECIAL = re.compile(r'[^\w\u4e00-\u9fff]')


def _tokenize_for_fts(text: str) -> str:
    """将文本分词后用空格连接，用于 FTS5 预分词存储"""
    return " ".join(_extract_fts_keywords(text))


def _extract_fts_keywords(text: str, *, min_length: int = 2) -> list[str]:
    """提取关键词用于 FTS5 索引和查询，jieba 优先，n-gram 降级"""
    has_cjk = bool(_CJK_RANGE.search(text))
    if has_cjk:
        try:
            import jieba
            raw_tokens = jieba.lcut_for_search(text)
        except ImportError:
            # n-gram 降级
            raw_tokens = [text[i:i+n] for n in range(2, 5) for i in range(len(text)-n+1)]
    else:
        raw_tokens = _KEYWORD_SPLIT.split(text.lower())

    seen = set()
    result = []
    for token in raw_tokens:
        token = token.strip()
        if len(token) >= min_length and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def _build_fts_query(query: str) -> str:
    """构建 FTS5 MATCH 查询字符串，关键词 OR 连接"""
    tokens = _extract_fts_keywords(query)
    quoted = []
    for token in tokens:
        cleaned = _FTS_SPECIAL.sub(" ", token).strip()
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted) if quoted else ""


def reciprocal_rank_fusion(ranked_lists: list[list[str]], *, k: int = 60, limit: int = 10) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: 多路排序融合算法"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
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
                 router=None, knowledge_graph=None, security_filter=None,
                 reranker=None, query_transformer=None):
        self.db = db
        self.memory = memory
        self.vec = vector_store
        self.router = router
        self.kg = knowledge_graph
        self._security_filter = security_filter
        self._reranker = reranker
        self._query_transformer = query_transformer
        self._last_message_time: float = 0
        self._last_encode_time: float = 0
        self._pending_encode = False

    def set_knowledge_graph(self, kg):
        self.kg = kg

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
        except Exception:
            pass
        return False

    def signal_new_message(self):
        self._last_message_time = time.time()
        self._pending_encode = True

    async def retrieve_memories_hybrid(self, query: str, k: int = 5, use_reranker: bool = True) -> list[dict]:
        """FTS + 向量 RRF 混合检索 + Reranker 精排

        Args:
            use_reranker: 是否在本方法内调用 Reranker 精排。A3 并行检索场景下会置为
                False，由调用方对合并后的候选池做一次性批量 Reranker。
        """
        fts_items = []
        vec_items = []

        # FTS 检索
        if self.memory:
            try:
                fts_items = await self.memory.search_memories_fts(query, limit=k * 2)
            except Exception as e:
                logger.warning("memory.fts_search_failed", error=str(e))

        # 向量检索
        if self.vec:
            try:
                vec_results = await self.vec.search(query, top_k=k * 2)
                # 批量 JOIN：一次查询获取所有向量命中的记忆记录
                if vec_results:
                    vec_ids = [row_id for row_id, _ in vec_results]
                    vec_mems = await self.memory.get_memories_by_ids(vec_ids)
                    # 构建 id -> memory 映射
                    vec_mem_map = {m["id"]: m for m in vec_mems}
                    # 按 distance 排序组装结果
                    for row_id, distance in vec_results:
                        mem = vec_mem_map.get(row_id)
                        if mem:
                            mem["score"] = 1.0 - distance
                            vec_items.append(mem)
            except Exception as e:
                logger.warning("memory.vec_search_failed", error=str(e))

        # 降级：只有一路有结果
        if not fts_items and not vec_items:
            return []
        if not fts_items:
            return vec_items[:k]
        if not vec_items:
            return fts_items[:k]

        # RRF 融合 - 过采样 3x 供 Reranker 筛选
        oversample_k = k * 3
        fts_ids = [str(item["id"]) for item in fts_items]
        vec_ids = [str(item["id"]) for item in vec_items]
        fused = reciprocal_rank_fusion([fts_ids, vec_ids], limit=oversample_k)

        # 按 RRF 排序获取完整记录
        score_by_id = dict(fused)
        all_items = {str(item["id"]): item for item in fts_items + vec_items}

        # Reranker 精排
        if use_reranker and self._reranker and self._reranker.available and len(fused) > k:
            docs = []
            idx_map = {}
            for i, (item_id, rrf_score) in enumerate(fused):
                if item_id in all_items:
                    docs.append(all_items[item_id].get("summary", ""))
                    idx_map[i] = item_id

            if docs:
                try:
                    reranked = await self._reranker.rerank(
                        query=query,
                        documents=docs,
                        top_n=k,
                    )
                    results = []
                    for item in reranked:
                        orig_idx = item["index"]
                        item_id = idx_map.get(orig_idx)
                        if item_id and item_id in all_items:
                            mem = all_items[item_id]
                            mem["rerank_score"] = item["relevance_score"]
                            mem["rrf_score"] = dict(fused).get(item_id, 0)
                            results.append(mem)
                    if results:
                        return results[:k]
                except Exception as e:
                    logger.warning("memory.rerank_failed", error=str(e))

        # 降级：无 Reranker 或 Reranker 失败时走原 RRF 逻辑
        results = []
        for item_id, rrf_score in fused:
            if item_id in all_items:
                item = all_items[item_id]
                item["rrf_score"] = rrf_score
                results.append(item)

        return results[:k]

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
        except Exception:
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

    async def retrieve_memories(self, query: str, k: int = 5, context: str = "") -> list[dict]:
        import config
        results = []

        # A1: 智能短路 - 简单查询跳过查询变换，直接走混合检索
        if getattr(config, "RETRIEVAL_SMART_SKIP", True) and self._is_retrieval_simple(query):
            return await self.retrieve_memories_hybrid(query, k=k)

        # 查询变换：改写 + 扩展
        queries = [query]
        if self._query_transformer and getattr(config, "QUERY_TRANSFORM_ENABLED", True):
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

        # 多查询检索
        all_results = []
        seen_ids = set()
        parallel_search = getattr(config, "RETRIEVAL_PARALLEL_SEARCH", True)

        if parallel_search and len(queries) > 1:
            # A3: 并行多查询检索 + 批量 Reranker
            # 各子查询检索时关闭内部 Reranker，统一在合并池上做一次批量精排
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
        else:
            # 串行降级（原有逻辑）
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

        results = all_results

        # 降级：纯向量检索
        if not results and self.vec:
            try:
                vec_results = await self.vec.search(query, top_k=k)
                # 批量 JOIN：一次查询获取所有向量命中的记忆记录
                if vec_results:
                    vec_ids = [row_id for row_id, _ in vec_results]
                    vec_mems = await self.memory.get_memories_by_ids(vec_ids)
                    # 构建 id -> memory 映射
                    vec_mem_map = {m["id"]: m for m in vec_mems}
                    # 按 distance 排序组装结果
                    for row_id, distance in vec_results:
                        mem = vec_mem_map.get(row_id)
                        if mem:
                            mem["score"] = 1.0 - distance
                            results.append(mem)
            except Exception as e:
                logger.warning("memory.vec_search_failed", error=str(e))

        # 最终兜底：重要性排序
        if not results and self.memory:
            try:
                results = await self.memory.search_memories_by_importance(
                    min_importance=0.4, limit=k
                )
            except Exception as e:
                logger.warning("memory.fallback_search_failed", error=str(e))

        # 流体记忆评分（艾宾浩斯遗忘曲线 + 访问强化）
        _fluid = FluidMemory()
        for r in results:
            created_at = r.get("timestamp", time.time())
            access_count = r.get("access_count", 0)
            similarity = r.get("score", 0.5)  # 向量相似度或 FTS 分数
            fluid_score = _fluid.score(similarity, created_at, access_count)
            if _fluid.should_filter(fluid_score):
                continue  # 过滤低分记忆
            importance = r.get("importance", 0.5)
            r["effective_score"] = importance * fluid_score
            # 检索强化：递增访问计数
            await self.memory.increment_access_count(r["id"])

        # KG 增强评分
        kg_boosts = []
        if self.kg and results:
            try:
                summaries = [r.get("summary", "") for r in results]
                kg_boosts = await self.kg.get_relevance_boost(query, summaries)
            except Exception as e:
                logger.debug("memory.kg_boost_failed", error=str(e))

        # 综合评分: final = α×rerank + β×kg_boost + γ×(importance×decay)
        alpha = getattr(config, "RAG_RERANK_WEIGHT", 0.65)
        beta = getattr(config, "RAG_KG_WEIGHT", 0.15)
        gamma = getattr(config, "RAG_IMPORTANCE_WEIGHT", 0.20)

        for i, r in enumerate(results):
            rerank_score = r.get("rerank_score", r.get("rrf_score", r.get("effective_score", 0.5)))
            kg_boost = kg_boosts[i] if i < len(kg_boosts) else 0.0
            importance_decay = r.get("effective_score", 0.5)
            r["final_score"] = alpha * rerank_score + beta * kg_boost + gamma * importance_decay

        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        results = results[:k]

        # KG 上下文增强（保留原有逻辑）
        if self.kg and results:
            try:
                entity_names = []
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

        return results

    async def encode_memory(self, context: dict):
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

            if self.vec and summary:
                await self.vec.upsert(mem_id, summary)

            self._last_encode_time = time.time()
            self._pending_encode = False
            logger.info("memory.encoded", summary=summary[:80], importance=importance)

            self._save_state_json(summary, importance, emotion)
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
                parts.append(f"用户: {content[:100]}")
            elif role == "assistant" and content:
                parts.append(f"助手: {content[:100]}")

        summary = " | ".join(parts)
        return summary[:500]

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

    async def try_idle_encode(self, context: dict, force: bool = False):
        now = time.time()
        if not self._pending_encode:
            return
        if not force and now - self._last_message_time < self.IDLE_THRESHOLD:
            return
        if now - self._last_encode_time < self.ENCODE_COOLDOWN:
            return

        await self.encode_memory(context)

    def _save_state_json(self, summary: str, importance: float, emotion: str):
        """原子写入记忆状态到 JSON 文件"""
        try:
            from pathlib import Path
            # 使用用户数据目录，避免写入 _MEIPASS 只读目录
            try:
                from config import MEMORY_STATE_DIR
                state_dir = MEMORY_STATE_DIR
            except ImportError:
                state_dir = Path(__file__).parent / "state"
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

    async def shutdown(self) -> str:
        if self.vec:
            await self.vec.close()
        return "done"
