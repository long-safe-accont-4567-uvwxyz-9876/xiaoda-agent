import asyncio
import re
import time
from loguru import logger

from database import DatabaseManager
from db_memory import MemoryDB
from vector_store import VectorStore
from atomic_write import atomic_json_write


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
                 router=None, knowledge_graph=None, security_filter=None):
        self.db = db
        self.memory = memory
        self.vec = vector_store
        self.router = router
        self.kg = knowledge_graph
        self._security_filter = security_filter
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

    async def retrieve_memories_hybrid(self, query: str, k: int = 5) -> list[dict]:
        """FTS + 向量 RRF 混合检索"""
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
                if vec_results:
                    for row_id, distance in vec_results:
                        mem = await self.memory.get_memory_by_id(row_id)
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

        # RRF 融合
        fts_ids = [str(item["id"]) for item in fts_items]
        vec_ids = [str(item["id"]) for item in vec_items]
        fused = reciprocal_rank_fusion([fts_ids, vec_ids], limit=k)

        # 按 RRF 排序获取完整记录
        score_by_id = dict(fused)
        all_items = {str(item["id"]): item for item in fts_items + vec_items}
        results = []
        for item_id, rrf_score in fused:
            if item_id in all_items:
                item = all_items[item_id]
                item["rrf_score"] = rrf_score
                results.append(item)

        return results[:k]

    async def retrieve_memories(self, query: str, k: int = 5) -> list[dict]:
        results = []

        # 优先混合检索
        try:
            results = await self.retrieve_memories_hybrid(query, k=k)
        except Exception as e:
            logger.warning("memory.hybrid_search_failed", error=str(e))

        # 降级：纯向量检索
        if not results and self.vec:
            try:
                vec_results = await self.vec.search(query, top_k=k)
                if vec_results:
                    for row_id, distance in vec_results:
                        mem = await self.memory.get_memory_by_id(row_id)
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

        now = time.time()
        for r in results:
            age_hours = (now - r.get("timestamp", 0)) / 3600
            importance = r.get("importance", 0.5)
            r["effective_score"] = importance * max(0.1, 1.0 - age_hours / 168)

        results.sort(key=lambda x: x.get("effective_score", 0), reverse=True)
        results = results[:k]

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
        from security import SecurityFilter
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
