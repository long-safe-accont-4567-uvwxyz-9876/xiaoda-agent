from typing import Any, ClassVar
import asyncio
import re
import time
from loguru import logger

from db.database import DatabaseManager
from db.db_memory import MemoryDB
# FTS5 分词工具从 db.fts_utils 导入 (打破 db <-> memory 循环); 这里 re-export
# 保持向后兼容 (其他模块仍可 `from memory.memory_manager import _tokenize_for_fts`)
from .vector_store import VectorStore
from .fsrs_model import FSRSModel, MemoryState, MemoryPhase, ReinforcementSignal, estimate_initial_difficulty, S_INIT
from .memory_distiller import MemoryDistiller
from .query_cache import QueryCache
from .retrieval_assessor import RetrievalAssessor
from utils.atomic_write import atomic_json_write
from config import get_agent_display_name


def _log_task_exception(task: asyncio.Task) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("memory.bg_task_failed", error=str(exc), error_type=type(exc).__name__)


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
    (re.compile(r"刚才|刚刚"), 0, 0),
    (re.compile(r"大前天"), 3, 1),
    (re.compile(r"前天"), 2, 1),
    (re.compile(r"昨天|昨日"), 1, 1),
    (re.compile(r"今天|今日"), 0, 1),
    (re.compile(r"上周"), 7, 7),
    (re.compile(r"上个月|上月"), 30, 30),
    (re.compile(r"前几天|前些天"), 1, 7),
    (re.compile(r"最近"), 0, 7),
]


def _parse_temporal_query(query: str) -> tuple[float, float] | None:
    """从用户查询中解析时间词，返回 [start_ts, end_ts] 时间戳区间（秒）。

    支持的格式：
    1. 相对时间词：刚才/刚刚/N小时前/N分钟前/昨天/前天/大前天/今天/上周/上个月/前几天/最近
    2. 绝对日期：7月15号/7月15日/12月1号
    3. 绝对日期+时段：7月15号早上7点/7月15号晚上/今天早上/昨天晚上
    4. 绝对日期+时间范围：7月15号早上7点到8点/7月15号7点到9点

    无时间词返回 None。
    """
    now = _datetime.datetime.now(_datetime.UTC).astimezone()

    # ── 1. 相对时间：N小时前 / N分钟前 ──
    m = re.search(r"(\d+)\s*小时前", query)
    if m:
        hours = int(m.group(1))
        start = now - _datetime.timedelta(hours=hours)
        return start.timestamp(), now.timestamp()

    m = re.search(r"(\d+)\s*分钟前", query)
    if m:
        minutes = int(m.group(1))
        start = now - _datetime.timedelta(minutes=minutes)
        return start.timestamp(), now.timestamp()

    if re.search(r"刚才|刚刚", query):
        start = now - _datetime.timedelta(minutes=30)
        return start.timestamp(), now.timestamp()

    # ── 2. 绝对日期解析：N月N号/N月N日 ──
    # 支持时段：早上(N-N点)/上午/中午/下午/晚上/凌晨
    _TIME_OF_DAY = {
        "凌晨": (0, 6),
        "早上": (6, 9),
        "早晨": (6, 9),
        "上午": (8, 12),
        "中午": (11, 14),
        "下午": (12, 18),
        "傍晚": (17, 20),
        "晚上": (18, 24),
        "夜间": (18, 24),
        "夜里": (18, 24),
        "深夜": (21, 24),
    }

    # 匹配 "N月N号"/"N月N日" 或 "N.N日"/"N.N号"（如 7.16日、7.16号）
    date_match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", query)
    if not date_match:
        date_match = re.search(r"(\d{1,2})\.(\d{1,2})\s*[号日]", query)
    if date_match:
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        year = now.year
        # 如果月份大于当前月，说明是去年
        if month > now.month or (month == now.month and day > now.day):
            year = now.year - 1
        try:
            base_date = _datetime.datetime(year, month, day, tzinfo=now.tzinfo)
        except ValueError:
            base_date = None

        if base_date:
            # 检查是否有具体小时范围："N点到N点"
            hour_range = re.search(r"(\d{1,2})\s*[点时:：]\s*(?:到|~|-|—)\s*(\d{1,2})\s*[点时:：]?", query)
            if hour_range:
                h_start = int(hour_range.group(1))
                h_end = int(hour_range.group(2))
                start = base_date.replace(hour=h_start, minute=0, second=0, microsecond=0)
                end = base_date.replace(hour=h_end, minute=59, second=59, microsecond=0)
                return start.timestamp(), end.timestamp()

            # 检查是否有具体小时："N点" / "N点N分"
            single_hour = re.search(r"(\d{1,2})\s*[点时:：]\s*(\d{1,2})?\s*分?", query)
            if single_hour and single_hour.group(1):
                h = int(single_hour.group(1))
                minute = int(single_hour.group(2)) if single_hour.group(2) else 0
                start = base_date.replace(hour=h, minute=minute, second=0, microsecond=0)
                end = start + _datetime.timedelta(hours=1)
                return start.timestamp(), end.timestamp()

            # 检查时段词
            for tod_name, (tod_start, tod_end) in _TIME_OF_DAY.items():
                if tod_name in query:
                    start = base_date.replace(hour=tod_start, minute=0, second=0, microsecond=0)
                    end = base_date.replace(hour=tod_end, minute=0, second=0, microsecond=0) if tod_end < 24 else base_date.replace(hour=23, minute=59, second=59, microsecond=0)
                    return start.timestamp(), end.timestamp()

            # 纯日期：整天
            start = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end = base_date.replace(hour=23, minute=59, second=59, microsecond=0)
            return start.timestamp(), end.timestamp()

    # ── 3. 相对日期 + 时段："今天早上" / "昨天晚上" ──
    _REL_DATE_MAP = {
        "今天": 0, "今日": 0,
        "昨天": 1, "昨日": 1,
        "前天": 2,
        "大前天": 3,
    }
    for rel_word, offset_days in _REL_DATE_MAP.items():
        if rel_word in query:
            base_date = (now - _datetime.timedelta(days=offset_days)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            # 优先检查具体小时范围（"7点到8点"比"早上"更精确）
            hour_range = re.search(r"(\d{1,2})\s*[点时:：]\s*(?:到|~|-|—)\s*(\d{1,2})\s*[点时:：]?", query)
            if hour_range:
                h_start = int(hour_range.group(1))
                h_end = int(hour_range.group(2))
                start = base_date.replace(hour=h_start, minute=0, second=0, microsecond=0)
                end = base_date.replace(hour=h_end, minute=59, second=59, microsecond=0)
                return start.timestamp(), end.timestamp()
            # 其次检查时段词（"早上"→6-9点）
            for tod_name, (tod_start, tod_end) in _TIME_OF_DAY.items():
                if tod_name in query:
                    start = base_date.replace(hour=tod_start, minute=0, second=0, microsecond=0)
                    end = base_date.replace(hour=tod_end, minute=0, second=0, microsecond=0) if tod_end < 24 else base_date.replace(hour=23, minute=59, second=59, microsecond=0)
                    return start.timestamp(), end.timestamp()
            # 纯相对日期（无时段）：整天
            start = base_date
            end = (now if offset_days == 0 else base_date.replace(hour=23, minute=59, second=59, microsecond=0))
            return start.timestamp(), end.timestamp()

    # ── 4. 纯相对时间词（原有逻辑）──
    for pattern, offset_days, span_days in _TEMPORAL_PATTERNS:
        if pattern.search(query):
            start_date = (now - _datetime.timedelta(days=offset_days + span_days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
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


def _normalize_score(score, default=0.0):
    """归一化分数到 0-1"""
    if score is None:
        return default
    try:
        val = float(score)
        return max(0.0, min(1.0, val))
    except (TypeError, ValueError):
        return default


class RuleBasedMemoryExtractor:
    """基于正则的即时记忆提取器"""

    _PATTERNS: ClassVar[list[tuple[str, re.Pattern, float, float]]] = [
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
    """归一化文本用于去重：合并空白+去除CJK标点+小写"""
    import re as _re
    # 去除CJK标点（中文逗号、句号、感叹号等）
    text = _re.sub(r'[\u3000-\u303f\uff00-\uffef]', '', text)
    # 合并空白为单空格
    text = _re.sub(r'\s+', ' ', text).strip()
    return text.casefold()


def _char_bigrams(text: str) -> set[str]:
    """提取字符 bigram 集合（用于相似度计算）。

    先归一化（去标点+小写），再取相邻2字符组成集合。
    用于 _find_similar_knowledge 的 Jaccard 相似度过滤。
    """
    text = _normalize_for_dedupe(text)
    if len(text) < 2:
        return set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _natural_time_desc(ts: float) -> str:
    """把时间戳转成自然中文时段描述，供 conversation_log 摘要使用。

    根因：原来用 `[HH:MM]` 日志格式，LLM 看到后会照搬到回复里
    （例如输出 `[13:59] 刚才小妲还在想……`）。改用自然中文时段+时间，
    LLM 没有方括号日志格式可模仿，回复会更口语化。
    """
    import time as _t
    lt = _t.localtime(ts)
    hour, minute = lt.tm_hour, lt.tm_min
    if 5 <= hour < 8:
        period = "清晨"
    elif 8 <= hour < 11:
        period = "上午"
    elif 11 <= hour < 13:
        period = "中午"
    elif 13 <= hour < 17:
        period = "下午"
    elif 17 <= hour < 19:
        period = "傍晚"
    elif 19 <= hour < 23:
        period = "晚上"
    else:
        period = "深夜"
    h12 = hour if hour <= 12 else hour - 12
    if minute == 0:
        return f"{period}{h12}点"
    if minute == 30:
        return f"{period}{h12}点半"
    if minute < 10:
        return f"{period}{h12}点过{minute}分"
    return f"{period}{h12}点{minute}分"


class MemoryManager:
    """管理情景记忆的编码、检索、去重与遗忘等核心流程。"""

    IDLE_THRESHOLD = 30
    ENCODE_COOLDOWN = 60

    def __init__(self, db: DatabaseManager, memory: MemoryDB,
                 vector_store: VectorStore | None = None,
                 router: Any | None=None, knowledge_graph: Any | None=None, security_filter: Any | None=None,
                 reranker: Any | None=None, query_transformer: Any | None=None,
                 governance: Any | None=None,
                 entity_extractor: Any | None=None,
                 entity_store: Any | None=None) -> None:
        self.db = db
        self.memory = memory
        self.vec = vector_store
        self.router = router
        self.kg = knowledge_graph
        self._security_filter = security_filter
        self._reranker = reranker
        self._query_transformer = query_transformer
        self._governance = governance
        self.entity_extractor = entity_extractor
        self.entity_store = entity_store
        self._kg_v2_engine: Any = None
        self._last_message_time: float = 0
        self._last_encode_time: float = 0
        self._pending_encode = False
        self._last_lazy_migrate_ts: float = 0
        # P3 记忆蒸馏器（使用硅基流动免费模型，失败降级到 router）
        self.distiller = MemoryDistiller(router=router)
        # 冷启动路由: 记忆计数缓存 (TTL 60s, 避免每次检索都 COUNT 全表)
        self._memory_count_cache: int | None = None
        self._memory_count_ts: float = 0
        # 查询语义缓存：基于嵌入向量余弦相似度匹配，命中则跳过完整检索流水线
        import config as _cfg
        self._query_cache = QueryCache(
            embed_func=self._get_query_embedding_func(),
            threshold=getattr(_cfg, 'QUERY_CACHE_THRESHOLD', 0.88),
            max_size=getattr(_cfg, 'QUERY_CACHE_MAX_SIZE', 256),
            ttl=getattr(_cfg, 'QUERY_CACHE_TTL', 300),
        )
        # CRAG 检索评估器：评估检索结果质量，低置信度时触发兜底策略
        self._assessor = RetrievalAssessor()
        # FSRS-DSR 模型实例（无状态纯计算，复用避免热路径重复创建）
        self._fsrs = FSRSModel()

        # 扩散激活引擎（第五路 RRF 通道）
        self.concept_graph = None
        self.spreading_engine = None
        try:
            from memory.concept_graph import ConceptGraph
            from memory.spreading_activation import SpreadingActivationEngine
            from memory.key_extractor import KeyExtractor
            from db.db_concept import ConceptDB
            if hasattr(self, 'db') and self.db and hasattr(self.db, '_conn') and self.db._conn is not None:
                concept_db = ConceptDB(self.db._conn)
                self._concept_db = concept_db
                self._key_extractor = KeyExtractor()
                self.concept_graph = ConceptGraph(concept_db, self._key_extractor)
                self.spreading_engine = SpreadingActivationEngine(
                    concept_db, self.vec, self._key_extractor)
                logger.info("memory.spreading_activation_enabled")
        except Exception as e:
            logger.warning("memory.spreading_activation_init_failed",
                          error=str(e))

        # Confirm/Correct 机制
        self.confirm_correct = None
        if self.concept_graph and self.spreading_engine:
            try:
                from memory.confirm_correct import ConfirmCorrect
                self.confirm_correct = ConfirmCorrect(
                    self._concept_db, self.spreading_engine, self.memory,
                    self._key_extractor)
                logger.info("memory.confirm_correct_enabled")
            except Exception as e:
                logger.warning("memory.confirm_correct_init_failed",
                              error=str(e))

    def _get_query_embedding_func(self):
        """返回查询嵌入函数（复用 VectorStore.embed），不可用时返回 None。

        VectorStore 未注入或未配置 embed_client 时 embed 返回空列表，
        QueryCache 会据此降级为禁用缓存。
        """
        if self.vec is not None:
            return self.vec.embed
        return None

    def set_knowledge_graph(self, kg: Any) -> None:
        self.kg = kg

    def set_kg_v2_engine(self, engine: Any) -> None:
        """注入 KGSearchEngine 实例 (KG v2 混合检索)。"""
        self._kg_v2_engine = engine

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

    async def _has_duplicate(self, summary: str, scope: Any | None = None) -> bool:
        """检查是否存在归一化后内容相同的已有记忆（只对 is_raw=0 的提炼知识生效）。

        mem0 SPEC 优化：原始记忆（is_raw=1）不去重，保证 append-only 可追溯。

        Args:
            scope: Scope 对象。传入时只在同 scope 内查重。
        """
        normalized = _normalize_for_dedupe(summary)
        if len(normalized) < 10:
            return False
        try:
            # 用 FTS 搜索相关记忆，然后精确匹配
            if scope is not None:
                # scope 过滤：只查 is_raw=0 的提炼知识
                candidates = await self.memory.search_memories_fts_scoped(
                    summary, scope=scope, limit=5, is_raw=0
                )
            else:
                candidates = await self.memory.search_memories_fts(summary, limit=5)
            for c in candidates:
                # 只对 is_raw=0 的记忆判断重复
                if c.get("is_raw", 0) == 0 and _normalize_for_dedupe(c.get("summary", "")) == normalized:
                    return True
            # FTS 无结果时也检查最近记忆
            recent = await self.memory.get_episodic_recent(limit=10)
            for r in recent:
                if r.get("is_raw", 0) == 0 and _normalize_for_dedupe(r.get("summary", "")) == normalized:
                    return True
        except (OSError, TypeError):
            logger.debug("memory_manager.is_duplicate_check_failed", exc_info=True)
        return False

    def signal_new_message(self) -> None:
        self._last_message_time = time.time()
        self._pending_encode = True

    async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                        use_reranker: bool = True,
                                        use_kg: bool = True,
                                        scope: Any | None = None,
                                        include_raw: bool = True) -> list[dict]:
        """FTS + 向量 + KG + 子chunk + 扩散 + 实体 六路 RRF 混合检索 + Reranker 精排

        mem0 SPEC 优化：
        - 新增第6路：EntityStore.recall_by_entities
        - 新增 Entity Boost：精排阶段加分
        - 新增 scope 过滤：user_id + agent_id 隔离
        - 新增 include_raw：是否包含 is_raw=1 的原始记忆

        冷启动三段路由 (工业标准, 对标 Dify/Coze):
        - cold (0条):  纯 FTS, 向量检索完全关闭 → 零 Embedding 开销
        - warm (1~10条): FTS + 向量低权重融合, 向量仅做补充
        - hot  (>10条):  FTS + 向量均衡融合 (原有行为)

        Args:
            scope: Scope 对象。None 时使用默认 Scope()。
            include_raw: False=只查提炼知识（is_raw=0），True=查所有记忆
            use_reranker: 是否在本方法内调用 Reranker 精排。A3 并行检索场景下会置为
                False，由调用方对合并后的候选池做一次性批量 Reranker。闲聊型查询
                也会置为 False 以节省 Reranker 调用成本。
            use_kg: 是否启用 KG 第三路召回。闲聊型查询置为 False 避免不必要的
                KG 检索开销。
        """
        # scope 默认值
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        _start = time.time()
        is_raw_filter = None if include_raw else 0

        # 候选集大小参数化（可通过 config 配置）
        import config as _cfg
        recall_limit = getattr(_cfg, 'RAG_RECALL_LIMIT', 50)  # 每路召回 Top-N
        rerank_limit = getattr(_cfg, 'RAG_RERANK_LIMIT', 50)   # RRF 融合后送 Reranker 的数量

        # KG v2 混合检索协程 (定义于早期返回之前, 确保 cold/warm/hot 所有路径均能召回 KG v2 事实)
        async def _kg_v2_recall() -> list[dict]:
            """KG v2: 直接返回 KG 事实/实体作为上下文候选。"""
            import config as _v2_cfg
            if not getattr(_v2_cfg, 'KG_V2_ENABLED', False) or not getattr(self, '_kg_v2_engine', None):
                return []
            try:
                results = await self._kg_v2_engine.search(query, top_k=recall_limit)
                if not results:
                    return []
                # 将 KG 事实格式化为 dict 供上下文使用
                formatted = []
                for r in results:
                    if r.get("type") == "relation":
                        formatted.append({
                            "summary": r.get("fact", ""),
                            "source": "kg_v2",
                            "rrf_score": r.get("rrf_score", 0),
                        })
                    elif r.get("type") == "entity":
                        summary_text = f"{r.get('name', '')}({r.get('kind', '')}): {r.get('summary', '')}"
                        formatted.append({
                            "summary": summary_text,
                            "source": "kg_v2",
                            "rrf_score": r.get("rrf_score", 0),
                        })
                return formatted
            except Exception as e:
                logger.debug("memory.kg_v2_recall_failed", error=str(e))
                return []

        # ── 冷启动路由: 判断用户记忆档位 ──
        tier = await self.get_memory_tier()
        is_cold = tier == "cold"
        is_warm = tier == "warm"

        # 冷用户: 仅 FTS (scope 过滤), 完全跳过向量检索 (零 Embedding 开销)
        # 但 FTS 无结果时仍尝试向量检索作为兜底（避免 cold_max > 0 时丢失向量召回）
        if is_cold:
            fts_items, kg_v2_items = await asyncio.gather(
                self._hybrid_fts_search_scoped(
                    query, recall_limit, scope, is_raw_filter),
                _kg_v2_recall(),
            )
            if fts_items:
                results = fts_items[:k]
                # KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 ID-based 去重)
                if kg_v2_items and len(results) < k:
                    results.extend(kg_v2_items[:k - len(results)])
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold", results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            # FTS 无结果，尝试向量兜底 + KG v2
            vec_items = await self._hybrid_vec_search(query, recall_limit, is_raw=is_raw_filter, scope=scope)
            if vec_items:
                results = vec_items[:k]
                if kg_v2_items and len(results) < k:
                    results.extend(kg_v2_items[:k - len(results)])
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold+vec_fallback", results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            # FTS + 向量均无结果, 仅返回 KG v2 事实 (若存在)
            if kg_v2_items:
                results = kg_v2_items[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier="cold+kg_v2_only", results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier="cold", results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []

        # 懒迁移：concept_nodes 数 < episodic_memories 数时触发（5分钟节流）
        if self.concept_graph and not is_cold:
            if time.time() - self._last_lazy_migrate_ts > 300:  # 5分钟
                try:
                    self._last_lazy_migrate_ts = time.time()
                    ep_count = await self.memory.get_episodic_count()
                    node_count = await self.spreading_engine.db.get_node_count()
                    if node_count < ep_count:
                        unmigrated = await self.memory.get_unmigrated_memories(limit=50)
                        if unmigrated:
                            await self.concept_graph.lazy_migrate(unmigrated, limit=50)
                except Exception as e:
                    logger.debug("memory.lazy_migrate_failed", error=str(e))

        # ── 温/热用户: 并行执行 FTS、向量、KG 三路检索 ──
        # ContextNest A1: 提取确定性 selector → 候选集, 向量检索在候选集内排序
        selectors = self._extract_deterministic_selectors(query)
        candidate_ids = await self._get_candidate_ids_by_selectors(
            selectors, limit=recall_limit * 6, scope=scope)
        if candidate_ids is not None:
            logger.debug("memory.deterministic_selector",
                         selector_keys=[sk for sk in selectors if sk != "has_selectors"],
                         candidate_count=len(candidate_ids))

        # KG 召回协程（KG 可用时启用第三路，失败/空结果自动降级为两路融合）
        async def _kg_recall() -> list[dict]:
            if not self.kg or not use_kg:
                return []
            try:
                related_names = await self.kg.recall_by_query(query, limit=recall_limit)
                if not related_names:
                    return []
                return await self.memory.search_memories_by_entities_scoped(
                    related_names, limit=recall_limit, scope=scope)
            except Exception as e:
                logger.debug("memory.kg_recall_failed", error=str(e))
                return []

        # ── 子chunk召回协程（父子Chunk RAG优化）──
        async def _child_recall() -> list[dict]:
            """子chunk FTS+Vec并行检索 → 映射到父chunk记录。"""
            import config as _child_cfg
            if not getattr(_child_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True):
                return []
            try:
                # 子chunk FTS + Vec 并行
                async def _child_vec_recall() -> list[int]:
                    if not self.vec or not self.vec.enabled:
                        return []
                    query_vec = await self.vec.embed(query)
                    if not query_vec:
                        return []
                    results = await self.vec.search_child(query_vec, top_k=recall_limit)
                    if not results:
                        return []
                    child_ids = [r["id"] for r in results]
                    return await self.memory.get_child_parent_ids(child_ids)

                child_fts_results, child_vec_parent_ids = await asyncio.gather(
                    self.memory.search_child_fts(query, recall_limit),
                    _child_vec_recall(),
                )

                # 合并 parent_ids（去重）
                parent_ids: set[int] = set()
                for r in child_fts_results:
                    parent_ids.add(r["parent_id"])
                for pid in child_vec_parent_ids:
                    parent_ids.add(pid)

                if not parent_ids:
                    return []

                # 获取父chunk完整记录
                parent_mems = await self.memory.get_memories_by_ids(list(parent_ids))
                # scope 后过滤：子chunk向量检索是全局的，需确保父记忆不跨用户泄露
                parent_mems = [pm for pm in parent_mems
                               if pm.get("user_id") == scope.user_id
                               and pm.get("agent_id") == scope.agent_id]
                for pm in parent_mems:
                    pm["child_recall"] = True
                return parent_mems
            except Exception as e:
                logger.debug("memory.child_recall_failed", error=str(e))
                return []

        fts_items, vec_items, kg_items, child_items, spread_items, entity_items, kg_v2_items = await asyncio.gather(
            self._hybrid_fts_search_scoped(query, recall_limit, scope, is_raw_filter),
            self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids, is_raw=is_raw_filter, scope=scope),
            _kg_recall(),
            _child_recall(),
            self._spreading_recall(query, recall_limit, scope=scope),
            self._entity_recall(query, scope, recall_limit),
            _kg_v2_recall(),
        )

        # 空通道自动剔除: 七路都空则 fallback 查原始记忆（蒸馏失败时兜底）
        if not fts_items and not vec_items and not kg_items and not child_items and not spread_items and not entity_items and not kg_v2_items:
            # Fallback: 用相同 FTS+Vec 检索，但 include_raw（is_raw=0 和 is_raw=1 都返回）
            raw_fts, raw_vec = await asyncio.gather(
                self._hybrid_fts_search_scoped(query, recall_limit, scope, is_raw_filter=None),
                self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids, is_raw=None, scope=scope),
            )
            raw_results = (raw_fts or []) + (raw_vec or [])
            if raw_results:
                # 去重 + 按 score 排序
                seen_ids: set = set()
                deduped: list = []
                for r in raw_results:
                    rid = r.get("id")
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        deduped.append(r)
                deduped.sort(key=lambda x: x.get("score", x.get("rrf_score", 0)), reverse=True)
                results = deduped[:k]
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier=f"{tier}+raw_fallback",
                            results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=0,
                        duration_ms=int((time.time() - _start) * 1000))
            return []
        # 仅 KG v2 有结果: 直接返回 (KG v2 事实已带 rrf_score, 无需补全)
        if not fts_items and not vec_items and not kg_items and not child_items and kg_v2_items:
            results = kg_v2_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        # 单路有结果: 补充 rrf_score 后直接返回（与多路融合保持字段一致）
        # 注意: KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 ID-based 去重)
        if not fts_items and not vec_items and not kg_items and not spread_items and not entity_items:
            for item in child_items:
                item.setdefault("rrf_score", item.get("score", 0.0))
            results = child_items[:k]
            if kg_v2_items and len(results) < k:
                results.extend(kg_v2_items[:k - len(results)])
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        if not fts_items and not vec_items and not child_items and not spread_items and not entity_items:
            for item in kg_items:
                item.setdefault("rrf_score", item.get("score", 0.0))
            results = kg_items[:k]
            if kg_v2_items and len(results) < k:
                results.extend(kg_v2_items[:k - len(results)])
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        if not fts_items and not kg_items and not child_items and not spread_items and not entity_items:
            for item in vec_items:
                item.setdefault("rrf_score", item.get("similarity", item.get("score", 0.0)))
            results = vec_items[:k]
            if kg_v2_items and len(results) < k:
                results.extend(kg_v2_items[:k - len(results)])
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        if not vec_items and not kg_items and not child_items and not spread_items and not entity_items:
            for item in fts_items:
                item.setdefault("rrf_score", item.get("score", 0.0))
            results = fts_items[:k]
            if kg_v2_items and len(results) < k:
                results.extend(kg_v2_items[:k - len(results)])
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        if not fts_items and not vec_items and not kg_items and not child_items and not entity_items:
            for item in spread_items:
                item.setdefault("rrf_score", item.get("spreading_score", item.get("score", 0.0)))
            results = spread_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results
        if not fts_items and not vec_items and not kg_items and not child_items and not spread_items:
            for item in entity_items:
                item.setdefault("rrf_score", item.get("score", 0.0))
            results = entity_items[:k]
            logger.info("memory.search", event="memory_search",
                        query=query[:100], tier=tier, results=len(results),
                        duration_ms=int((time.time() - _start) * 1000))
            return results

        # ── 加权 RRF 融合（六路，空通道自动剔除） ──
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

        oversample_k = rerank_limit  # RRF 融合后取 Top-N 送 Reranker
        fts_ids = [str(item["id"]) for item in fts_items]
        vec_ids = [str(item["id"]) for item in vec_items]
        # 构建多路 ID 列表（KG/子chunk/扩散/实体 通道无结果时自动降级）
        ranked_lists = [fts_ids, vec_ids]
        weights = [fts_weight, vec_weight]
        if kg_items:
            for _kitem in kg_items:
                _kitem["kg_recall"] = True
            kg_ids = [str(item["id"]) for item in kg_items]
            ranked_lists.append(kg_ids)
            weights.append(0.8)  # KG 通道权重
        if child_items:
            child_ids = [str(item["id"]) for item in child_items]
            ranked_lists.append(child_ids)
            weights.append(0.9)  # 子chunk召回的父chunk权重
        if spread_items:
            spread_ids = [str(item["id"]) for item in spread_items]
            ranked_lists.append(spread_ids)
            weights.append(0.85)  # 扩散激活权重略低于直接匹配
        if entity_items:
            entity_ids = [str(item["id"]) for item in entity_items]
            ranked_lists.append(entity_ids)
            weights.append(0.7)  # 实体召回权重
        fused = reciprocal_rank_fusion(
            ranked_lists, limit=oversample_k,
            weights=weights,
        )

        # 按 RRF 排序获取完整记录（合并所有通道候选）
        # 注意: 同一 id 在多通道出现时需合并标记（如 kg_recall），避免后通道覆盖前通道标记
        all_items: dict[str, dict] = {}
        for item in fts_items + vec_items + kg_items + child_items + spread_items + entity_items:
            key = str(item["id"])
            if key in all_items:
                existing = all_items[key]
                # 合并布尔标记，任一通道命中即为 True
                for mark_key in ("kg_recall", "child_recall"):
                    if item.get(mark_key):
                        existing[mark_key] = True
                # 保留较高的 score（各通道归一化方式不同，取最大值更安全）
                if item.get("score", 0) > existing.get("score", 0):
                    existing["score"] = item["score"]
            else:
                all_items[key] = item

        # ── mem0 SPEC: Entity Boost 精排加分 ──
        candidates = []
        for item_id, rrf_score in fused:
            if item_id in all_items:
                item = all_items[item_id]
                item["rrf_score"] = rrf_score
                candidates.append(item)
        candidates = await self._apply_entity_boost(query, candidates, scope)

        # Reranker 精排
        if use_reranker and self._reranker and self._reranker.available and len(candidates) > k:
            reranked = await self._hybrid_rerank(query, fused, all_items, k)
            if reranked:
                # 对 reranked 也应用 entity boost
                reranked = await self._apply_entity_boost(query, reranked, scope)
                # KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 Reranker ID-based 排序)
                # 先切片再追加, 避免 reranker 返回 k 条时 [:k] 丢弃全部 kg_v2_items
                results = reranked[:k]
                if kg_v2_items and len(results) < k:
                    results.extend(kg_v2_items[:k - len(results)])
                logger.info("memory.search", event="memory_search",
                            query=query[:100], tier=tier, results=len(results),
                            duration_ms=int((time.time() - _start) * 1000))
                return results

        # 降级：无 Reranker 或 Reranker 失败时走 candidates (已含 entity boost)
        final = candidates[:k]
        # KG v2 事实作为补充候选追加 (已带 rrf_score, 不参与 ID-based 去重)
        # 先切片再追加, 确保至少有部分 kg_v2 命中能露出
        if kg_v2_items and len(final) < k:
            final.extend(kg_v2_items[:k - len(final)])
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
                                 candidate_ids: list[int] | None = None,
                                 is_raw: int | None = None,
                                 scope: Any | None = None) -> list[dict]:
        """向量检索 + 批量 JOIN：一次查询获取所有向量命中的记忆记录

        ContextNest A1: candidate_ids 提供时, 向量检索只在确定性候选集内排序,
        候选集本身由 metadata selector (时间/重要性) 产生, Jaccard 1.0。
        is_raw: None=不过滤, 0=只查蒸馏知识, 1=只查原始记忆
        scope: 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
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
            if is_raw is not None:
                vec_mems = [m for m in vec_mems if m.get("is_raw") == is_raw]
            if scope is not None:
                vec_mems = [m for m in vec_mems
                            if m.get("user_id") == scope.user_id
                            and m.get("agent_id") == scope.agent_id]
            # 构建 id -> memory 映射，按 distance 排序组装结果
            vec_mem_map = {m["id"]: m for m in vec_mems}
            # 只用过滤后的距离计算 max_dist，避免被过滤记忆的距离污染归一化
            filtered_dists = [d for rid, d in vec_results if rid in vec_mem_map]
            items = []
            if filtered_dists:
                if len(filtered_dists) == 1:
                    _use_normalize = False
                    max_dist = 0.0
                else:
                    max_dist = max(filtered_dists)
                    _use_normalize = max_dist > 0
            else:
                _use_normalize = False
                max_dist = 0.0
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

    async def _spreading_recall(self, query: str, limit: int,
                                scope: Any | None = None) -> list[dict]:
        """扩散激活第五路检索通道

        通过 SpreadingActivationEngine 检索 concept_nodes，
        将结果映射回 episodic_memories（通过 source_mem_id）。
        scope 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
        """
        if not self.spreading_engine:
            return []
        try:
            results = await self.spreading_engine.recall(query, top_k=limit)
            if not results:
                return []
            # 映射回 episodic_memories，多 node 指向同一 memory 时取最高分
            mem_ids = []
            for r in results:
                node = await self.spreading_engine.db.get_node(r["id"])
                if node and node.get("source_mem_id"):
                    mem_ids.append((node["source_mem_id"], r["score"]))
            if not mem_ids:
                return []
            # 批量获取记忆
            ids = [m[0] for m in mem_ids]
            # 多 node 指向同一 memory 时保留最高分（取 max 而非覆盖）
            score_map: dict[int, float] = {}
            for mid, score in mem_ids:
                if mid not in score_map or score > score_map[mid]:
                    score_map[mid] = score
            memories = await self.memory.get_memories_by_ids(ids)
            if scope is not None:
                memories = [m for m in memories
                            if m.get("user_id") == scope.user_id
                            and m.get("agent_id") == scope.agent_id]
            for mem in memories:
                mem["spreading_score"] = score_map.get(mem["id"], 0.0)
                mem["spreading_recall"] = True
            return memories
        except Exception as e:
            logger.debug("memory.spreading_recall_failed", error=str(e))
            return []

    def _extract_deterministic_selectors(self, query: str) -> dict[str, Any]:
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
                                                limit: int = 200,
                                                scope: Any | None = None) -> list[int] | None:
        """根据确定性 selector 查询候选 rowid 集合。

        无 selector 返回 None (调用方走原 KNN 全量检索)。
        scope 非空时追加 user_id/agent_id 过滤，防止跨用户候选泄露。
        """
        if not selectors.get("has_selectors"):
            return None
        clauses: list[str] = []
        params: list = []
        if scope is not None:
            clauses.append("user_id = ?")
            clauses.append("agent_id = ?")
            params.extend([scope.user_id, scope.agent_id])
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

    def _suggest_k(self, query: str, default_k: int = 8) -> int:
        """根据查询内容智能建议检索条数 k（情感陪伴型 bot）。

        策略：
        - 极短闲聊（问候/确认）：k=2，避免注入无关记忆
        - 日常闲聊：k=5~8
        - 情感/回忆/个人话题：k=10，多检索相关情感记忆
        - 涉及具体事件/人物/经历：k=10，召回更多上下文
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

        # 情感/回忆/个人话题 → 多检索，让回复更有温度和连贯性
        # 注意：必须在长度检查之前，否则短查询会被提前截断
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
                return min(10, default_k + 2)

        # 涉及具体事件/人物/经历
        event_indicators = (
            "发生", "那次", "那件事", "什么时候", "哪里", "谁",
            "聊天", "说过", "告诉你", "跟我说", "你记得",
            "上次", "上次说", "之前说", "你说过",
        )
        for indicator in event_indicators:
            if indicator in query_lower:
                return min(10, default_k + 1)

        # 极短查询：问候、确认、单字回复
        if effective_len <= 8:
            return 2

        # 短查询：简单闲聊
        if effective_len <= 15:
            return 5

        # 长查询：可能涉及多话题
        if effective_len > 60:
            return min(10, default_k + 2)

        return default_k

    async def retrieve_memories(self, query: str, k: int = 5, context: str = "",
                                 _retry_attempted: bool = False,
                                 scope: Any | None = None) -> list[dict]:
        import config
        if scope is None:
            from memory.scope import Scope
            scope = Scope()
        # 查询语义缓存：命中则直接返回，跳过完整检索流水线
        if getattr(config, 'QUERY_CACHE_ENABLED', True):
            logger.debug("memory.retrieve_stage", stage="query_cache_get", query=query[:50])
            cached = await self._query_cache.get(query)
            if cached is not None:
                logger.info("memory.cache_hit", query=query[:100])
                return cached

        # 意图路由：按查询意图调整 k 与检索通道（闲聊型跳过 KG/Reranker）
        # A4 根本修复：移除外层 asyncio.wait_for，因为 query_transform.py 内部已有超时控制
        # 双重超时（外层5s + 内层5s）会导致不必要的失败
        logger.debug("memory.retrieve_stage", stage="classify_intent", query=query[:50])
        intent = "factual"
        if self._query_transformer and self._query_transformer.available:
            try:
                intent = await self._query_transformer.classify_intent(query)
            except Exception as e:
                logger.debug("memory_manager.classify_intent_failed", error=str(e))
                intent = "factual"

        # 按意图调整 k（宽松策略：不主动缩小k，避免丢失结果）
        if intent == "multi-hop":
            k = max(k, 8)
        # chat/factual/temporal 保持原 k

        # 时间实体识别：检测"昨天/前天/上周"等时间词，按时间范围检索
        # 这让小妲能回答"昨天发生了什么"这类纯时间查询
        # 修复：时间检索返回空时不短路，继续走语义检索兜底，避免"不知道/忘记了"
        logger.debug("memory.retrieve_stage", stage="temporal_search", query=query[:50])
        temporal_results = await self._try_temporal_search(query, k, scope=scope, include_raw=True)
        if temporal_results:
            # 时间检索命中也递增 access_count（与常规检索路径一致）
            hit_ids = [r.get("id") for r in temporal_results if r.get("id")]
            if hit_ids:
                _bg = asyncio.create_task(
                    self._batch_touch_memories(hit_ids),
                    name=f"touch_temporal_{int(time.time()*1000)}"
                )
                _bg.add_done_callback(_log_task_exception)
            return temporal_results

        # A1: 智能短路 - 简单查询跳过查询变换，直接走混合检索
        if getattr(config, "RETRIEVAL_SMART_SKIP", True) and self._is_retrieval_simple(query):
            # 闲聊型查询跳过 KG 和 Reranker，节省检索成本
            use_reranker = intent != "chat"
            use_kg = intent != "chat"
            results = await self.retrieve_memories_hybrid(
                query, k=k, use_reranker=use_reranker, use_kg=use_kg, scope=scope)
            if results:
                # 简单路径使用与复杂路径一致的评分逻辑，保证评分尺度统一
                results = await self._apply_fsrs_scoring(results)
                query_entities: set[str] = set()
                if self.kg:
                    try:
                        query_entities = await self.kg.get_query_entities(query)
                    except Exception:
                        logger.debug("memory_manager.query_entities_failed", exc_info=True)
                await self._compute_final_scores(query, results, config, query_entities)

                # CRAG 检索评估（A4 根本修复：闲聊型查询跳过 CRAG 评估）
                # 闲聊型查询不需要精确检索，CRAG 评估会产生不必要的低置信度告警
                if intent != "chat":
                    assessment = self._assessor.assess(query, results)
                    if assessment["should_retry"] and not _retry_attempted:
                        logger.info("memory.crag_low_confidence",
                                    query=query[:100], confidence=assessment["confidence"])
                        # 扩大候选集重试一次
                        retry_k = k * 2
                        retry_results = await self.retrieve_memories_hybrid(
                            query, k=retry_k, use_reranker=True, use_kg=True, scope=scope)
                        if retry_results:
                            retry_results = await self._apply_fsrs_scoring(retry_results)
                            await self._compute_final_scores(query, retry_results, config, query_entities)
                            retry_results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
                            results = retry_results[:k]
                            # 重新评估
                            reassessment = self._assessor.assess(query, results)
                            logger.info("memory.crag_retry_done",
                                        confidence=reassessment["confidence"],
                                        level=reassessment["level"])

                results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
                results = results[:k]

            # 注：移除 importance fallback
            # 根因：空结果时按重要性排序返回 k 条，完全不做语义匹配，
            # 会注入"重要但无关"的记忆（如用户问天气却返回上次生日记忆）。
            # 空结果应如实返回空，由模型调 recall 工具或如实说"不记得"。

            # 内容相似度去重（与复杂路径保持一致，避免多通道 RRF 融合后返回近似重复）
            if results:
                results = self._dedup_by_content_similarity(results)

            # 写入缓存
            if getattr(config, 'QUERY_CACHE_ENABLED', True) and results:
                await self._query_cache.put(query, results)
            return results

        # 查询变换：改写 + 扩展
        queries = await self._transform_queries(query, context)

        # 多查询检索
        if getattr(config, "RETRIEVAL_PARALLEL_SEARCH", True) and len(queries) > 1:
            all_results = await self._multi_query_parallel_search(queries, query, k, scope=scope)
        else:
            all_results = await self._multi_query_serial_search(queries, k, scope=scope)
        results = all_results

        # 降级：纯向量检索
        if not results:
            results = await self._vector_fallback_search(query, k, scope=scope)

        # 注：移除 importance fallback（同上，会注入"重要但无关"的记忆）
        # 空结果如实返回空，由模型调 recall 工具或如实说"不记得"

        # 流体记忆评分（艾宾浩斯遗忘曲线 + 访问强化）
        results = await self._apply_fsrs_scoring(results)

        # 保留实体提取用于评分增强，但不再后置追加候选
        # （KG 召回已前移到 retrieve_memories_hybrid 的并行召回阶段，统一走 RRF + Reranker）
        query_entities: set[str] = set()
        if self.kg:
            try:
                query_entities = await self.kg.get_query_entities(query)
            except Exception as e:
                logger.debug("memory.query_entities_failed", error=str(e))

        # KG 增强评分 + 综合评分 (复用已提取的 query_entities, 避免 N+1 LLM)
        await self._compute_final_scores(query, results, config, query_entities)

        # CRAG 检索评估（A4 根本修复：闲聊型查询跳过 CRAG 评估）
        if intent != "chat":
            assessment = self._assessor.assess(query, results)
            if assessment["should_retry"] and not _retry_attempted:
                logger.info("memory.crag_low_confidence",
                            query=query[:100], confidence=assessment["confidence"])
                # 扩大候选集重试一次
                retry_k = k * 2
                retry_results = await self.retrieve_memories_hybrid(
                    query, k=retry_k, use_reranker=True, use_kg=True, scope=scope)
                if retry_results:
                    retry_results = await self._apply_fsrs_scoring(retry_results)
                    await self._compute_final_scores(query, retry_results, config, query_entities)
                    retry_results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
                    results = retry_results[:k]
                    # 重新评估
                    reassessment = self._assessor.assess(query, results)
                    logger.info("memory.crag_retry_done",
                                confidence=reassessment["confidence"],
                                level=reassessment["level"])

            # 注：移除 importance fallback（同上，会注入"重要但无关"的记忆）

        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        results = results[:k]

        # 主动检索 A：话题触发器
        # 从 query 抽取 top-N 话题关键词，对每个词做轻量 FTS 检索，
        # 把"主题相关但未被主路命中"的记忆补充进来，扩大主动联想。
        # 这样即使主路 RRF 没召回，话题相关的旧记忆也能浮上来。
        # 修复 P1-3：本函数不再内部截断，topic_hits 会以 final_score=0.25 保留在末尾，
        # 由下面的统一截断处理。
        results = await self._apply_topic_trigger(query, results, k, scope=scope)

        # KG 上下文增强（保留原有逻辑）
        await self._apply_kg_context_enhance(results)

        results = self._dedup_by_content_similarity(results)

        # 修复 P1-3：话题触发器修复后的统一截断
        # 允许最多 k+2 条结果（k 条主路 + 最多 2 条话题触发补充），让话题触发记忆可见。
        # 如果没有 topic_hits，截断到 k；有则保留 k+2 上限。
        _has_topic = any(r.get("topic_trigger") for r in results)
        _final_k = k + 2 if _has_topic else k
        if len(results) > _final_k:
            results = results[:_final_k]

        # 写入缓存
        if getattr(config, 'QUERY_CACHE_ENABLED', True) and results:
            await self._query_cache.put(query, results)

        # 检索命中后批量递增 access_count（passive_use）
        # 修复：此前 increment_access_count 从未被调用，导致记忆永远无法进入 PERMANENT 状态
        # 这里使用 fire-and-forget 方式，不阻塞检索返回
        if results:
            hit_ids = [r.get("id") for r in results if r.get("id")]
            if hit_ids:
                _bg = asyncio.create_task(
                    self._batch_touch_memories(hit_ids),
                    name=f"touch_memories_{int(time.time()*1000)}"
                )
                _bg.add_done_callback(_log_task_exception)
        return results

    async def _try_temporal_search(self, query: str, k: int,
                                    scope: Any | None = None,
                                    include_raw: bool = False) -> list[dict] | None:
        """时间型查询：直接查 conversation_logs 原始对话。

        根本修复：时间查询最需要的是完整的原始对话记录，不是经过 FTS/reranker/CRAG
        多层管线过滤后的蒸馏摘要。conversation_logs 是最可靠、最完整的数据源。

        查找顺序：conversation_logs → episodic_memories（兜底）
        无时间词返回 None（调用方继续走常规语义检索）。
        """
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        _time_range = _parse_temporal_query(query)
        if not _time_range:
            return None
        start_ts, end_ts = _time_range
        try:
            # 第一优先：直接查 conversation_logs 原始对话（最可靠）
            # 时间查询用户要的是"发生了什么"，原始对话比蒸馏摘要更准确
            _conv_results = await self._search_conversation_logs(
                start_ts, end_ts, scope, k * 4)
            if _conv_results:
                logger.debug("memory.temporal_convlogs_hit",
                             query=query[:50], count=len(_conv_results))
                return _conv_results

            # 兜底：conversation_logs 无结果时查 episodic_memories
            # （可能对话还没来得及记录，但蒸馏记忆已生成）
            is_raw_filter = None if include_raw else 0
            _time_results = await self.memory.search_memories_by_time_scoped(
                start_ts, end_ts, scope=scope, limit=k * 2, is_raw=is_raw_filter
            )
            if _time_results:
                logger.debug("memory.temporal_episodic_hit",
                             query=query[:50], count=len(_time_results))
                return _time_results
            # 两级 fallback：含 is_raw=1 的原始记录
            if is_raw_filter is not None:
                _fallback_results = await self.memory.search_memories_by_time_scoped(
                    start_ts, end_ts, scope=scope, limit=k * 2, is_raw=None
                )
                if _fallback_results:
                    logger.debug("memory.temporal_fallback_raw_hit",
                                 query=query[:50], count=len(_fallback_results))
                    return _fallback_results
            return []
        except Exception as e:
            logger.warning("memory.temporal_search_failed", error=str(e))
            return None

    async def _search_conversation_logs(self, start_ts: float, end_ts: float,
                                         scope: Any | None, k: int) -> list[dict]:
        """查 conversation_logs 原始对话，格式化为记忆格式返回。

        不做 user_id 过滤（conversation_logs 的 user_id 是 QQ/微信等外部 ID，
        与 scope.user_id='default' 不匹配），直接按时间范围查全部对话。
        """
        import time as _time
        try:
            # 不传 user_id，查时间范围内的所有对话
            raw = await self.memory.get_conversations_by_time_range(
                start_ts, end_ts, user_id="", limit=k
            )
            if not raw:
                return []
            results = []
            for row in raw:
                ts = row.get("timestamp", 0)
                user_msg = (row.get("user_message") or "")
                asst_msg = (row.get("assistant_reply") or "")
                if not user_msg and not asst_msg:
                    continue
                # 场景指令检测：用户有时发送"（场景：...格式：...）"这类
                # 元指令来控制 agent 行为，不是真正的对话内容。LLM 在回忆时
                # 会原样复述这些指令（系统 prompt 泄漏），所以需要标记为
                # "场景指令"，让 LLM 知道这不是需要复述给用户听的内容。
                if user_msg.startswith("（场景：") or user_msg.startswith("(场景："):
                    user_msg = "（场景指令，非对话内容，回忆时不要复述）"
                # 带完整日期的时间锚点 + 叙事化格式：根因修复
                # 之前格式"时间：...\n爸爸：...\n小妲：..."像数据记录，LLM 模仿输出
                # "时间线整理：⏰ 约7:09"等出戏格式。改为叙事性格式——像回忆的画面
                # 浮现，而不是日志条目。LLM 看到叙事性内容，回忆时也会用叙事性语言。
                # 同时带完整年月日，防止 LLM 被记忆内容里的日期干扰（如用户当时
                # 在回忆"7月16日"，LLM 会采用内容里的日期作为锚点）。
                if ts:
                    from datetime import datetime as _dt_cls
                    _dt = _dt_cls.fromtimestamp(float(ts))
                    _period = _natural_time_desc(float(ts))
                    time_str = f"{_dt.year}年{_dt.month}月{_dt.day}日{_period}"
                else:
                    time_str = "某时"
                # 叙事化：用"——"连接时间和对话，用"爸爸说""你回答"代替"爸爸：""小妲："
                # 这种格式让 LLM 觉得这是回忆片段，不是数据记录
                summary = f"{time_str}——\n爸爸说：{user_msg}"
                if asst_msg:
                    summary += f"\n你当时回答：{asst_msg}"
                results.append({
                    "summary": summary,
                    "timestamp": ts,
                    "importance": 0.5,
                    "type": "conversation_log",
                    "is_raw": 1,
                    "user_id": scope.user_id if scope else "",
                    "agent_id": scope.agent_id if scope else "",
                })
            return results[:k]
        except Exception as e:
            logger.warning("memory.convlogs_search_failed", error=str(e))
            return []

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
                                             k: int,
                                             scope: Any | None = None) -> list[dict]:
        """A3: 并行多查询检索 + 批量 Reranker。

        各子查询检索时关闭内部 Reranker，统一在合并池上做一次批量精排。
        """
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        hybrid_tasks = [
            self.retrieve_memories_hybrid(q, k=k * 2, use_reranker=False, scope=scope)
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

    async def _multi_query_serial_search(self, queries: list[str], k: int,
                                           scope: Any | None = None) -> list[dict]:
        """串行降级（原有逻辑）。"""
        all_results: list[dict] = []
        seen_ids: set[str] = set()
        for q in queries:
            try:
                hybrid_results = await self.retrieve_memories_hybrid(q, k=k, scope=scope)
                for r in hybrid_results:
                    rid = str(r.get("id", ""))
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        all_results.append(r)
            except Exception as e:
                logger.warning("memory.hybrid_search_failed", query=q[:50], error=str(e))
        return all_results

    async def _vector_fallback_search(self, query: str, k: int,
                                       scope: Any | None = None) -> list[dict]:
        """降级：纯向量检索 + 批量 JOIN。

        scope 非空时后过滤 user_id/agent_id，防止跨用户记忆泄露。
        """
        if not self.vec:
            return []
        results: list[dict] = []
        try:
            vec_results = await self.vec.search(query, top_k=k)
            if vec_results:
                vec_ids = [row_id for row_id, _ in vec_results]
                vec_mems = await self.memory.get_memories_by_ids(vec_ids)
                # scope 后过滤：向量索引是全局的，需确保不跨用户泄露
                if scope is not None:
                    vec_mems = [m for m in vec_mems
                                if m.get("user_id") == scope.user_id
                                and m.get("agent_id") == scope.agent_id]
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

    async def _importance_fallback_search(self, k: int,
                                           scope: Any | None = None) -> list[dict]:
        """最终兜底：按重要性排序检索。

        scope 非空时按 user_id/agent_id 过滤，防止跨用户记忆泄露。
        """
        if not self.memory:
            return []
        try:
            return await self.memory.search_memories_by_importance_scoped(
                min_importance=0.4, limit=k, scope=scope
            )
        except Exception as e:
            logger.warning("memory.fallback_search_failed", error=str(e))
            return []

    async def _apply_fsrs_scoring(self, results: list[dict]) -> list[dict]:
        """FSRS-DSR 记忆评分（遗忘曲线 R + 状态过滤），过滤低分记忆。

        优化：
        1. 懒迁移 phase：检索时实时检查 phase 是否需要更新（BUFFER→DECAY/REINFORCED），
           无需后台任务
        2. 过滤阈值从 R<0.05 放宽到 R<0.01，避免过早遗忘有用记忆
        3. 检索命中后通过 _batch_touch_memories 异步递增 access_count 和 reinforcement_count
        """
        if not results:
            return results
        now = time.time()
        _migration_needed: list[tuple[int, str, float, int]] = []  # (id, phase, stability, rc)
        filtered: list[dict] = []
        for r in results:
            similarity = r.get("score", 0.5)
            last_review = r.get("last_review", 0.0)
            created_at = r.get("created_at", 0.0) or r.get("timestamp", 0.0)
            if last_review == 0.0:
                last_review = r.get("timestamp", 0.0)
                logger.debug("fsrs.last_review_fallback id={} using timestamp={}",
                             r.get("id"), last_review)
            try:
                phase = MemoryPhase.safe(r.get("phase", "buffer"))
            except ValueError:
                logger.warning("fsrs_invalid_phase id={} phase={}", r.get("id"), r.get("phase"))
                phase = MemoryPhase.BUFFER
            difficulty = r.get("difficulty", 5.0)
            stability = r.get("stability", 3.0)
            rc = r.get("reinforcement_count", 0)
            state = MemoryState(
                difficulty=difficulty,
                stability=stability,
                phase=phase,
                last_review=last_review,
                created_at=created_at,
                reinforcement_count=rc,
            )

            # 懒迁移：检查 phase 是否需要更新
            # FSRS transition: 21天后 BUFFER→DECAY(rc=0) 或 REINFORCED(rc>0)
            new_phase = self._fsrs._compute_phase(difficulty, stability, state, now)
            if new_phase != phase:
                phase = new_phase
                state = MemoryState(
                    difficulty=difficulty, stability=stability,
                    phase=phase, last_review=last_review,
                    created_at=created_at, reinforcement_count=rc,
                )
                mem_id = r.get("id")
                if mem_id:
                    _migration_needed.append((mem_id, phase.value, difficulty, stability, last_review, rc))

            R = state.retrievability(now)
            fsrs_score = self._fsrs.score(similarity, state, now)
            # 放宽过滤阈值：R < 0.01 才完全过滤（原 0.05 过于激进，会过早遗忘有用记忆）
            if R < 0.01:
                logger.debug("fsrs.filtered_out id={} R={:.4f} phase={}",
                             r.get("id"), R, phase.value)
                continue
            r["fluid_score"] = R
            r["fsrs_score"] = fsrs_score
            importance = r.get("importance", 0.5)
            r["effective_score"] = importance * fsrs_score
            filtered.append(r)

        # 异步批量迁移 phase（fire-and-forget，不阻塞检索返回）
        if _migration_needed:
            _bg = asyncio.create_task(
                self._batch_migrate_phase(_migration_needed),
                name=f"fsrs_migrate_{int(now*1000)}"
            )
            _bg.add_done_callback(_log_task_exception)
        return filtered

    async def _batch_migrate_phase(self, migrations: list[tuple[int, str, float, float, float, int]]) -> None:
        """异步批量迁移记忆 phase（懒迁移的持久化部分）。

        Args:
            migrations: (mem_id, phase, difficulty, stability, last_review, reinforcement_count)
        """
        try:
            for mem_id, phase, difficulty, stability, last_review, rc in migrations:
                try:
                    await self.memory.update_fsrs_state(
                        mem_id,
                        difficulty=difficulty,
                        stability=stability,
                        phase=phase,
                        last_review=last_review,
                        reinforcement_count=rc,
                    )
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("fsrs.migrate_failed", mid=mem_id, error=str(e))
            logger.debug("fsrs.batch_migrated", count=len(migrations))
        except Exception as e:
            logger.warning("fsrs.batch_migrate_error", error=str(e))

    def _dedup_by_content_similarity(self, results: list[dict], threshold: float = 0.7) -> list[dict]:
        if len(results) <= 1:
            return results
        kept = []
        for r in results:
            r_bigrams = _char_bigrams(r.get("summary", ""))
            is_dup = False
            for k in kept:
                k_bigrams = _char_bigrams(k.get("summary", ""))
                if not r_bigrams or not k_bigrams:
                    continue
                jaccard = len(r_bigrams & k_bigrams) / len(r_bigrams | k_bigrams)
                if jaccard > threshold:
                    r_is_distilled = r.get("is_raw", 1) == 0
                    k_is_distilled = k.get("is_raw", 1) == 0
                    if r_is_distilled and not k_is_distilled:
                        kept.remove(k)
                        break
                    elif k_is_distilled and not r_is_distilled:
                        is_dup = True
                        break
                    elif r.get("final_score", 0) <= k.get("final_score", 0):
                        is_dup = True
                        break
                    else:
                        kept.remove(k)
                        break
            if not is_dup:
                kept.append(r)
        return kept

    def _compute_recency_boost(self, item: dict) -> float:
        """计算时间新鲜度加成 (0-1)。

        1.0 = 1小时内，0.0 = 很久以前。无时间信息给中等偏低值 0.3。
        小时级粒度，避免同一天内的记忆无法区分新鲜度。
        """
        ts = item.get("timestamp") or item.get("created_at") or item.get("updated_at")
        if not ts:
            return 0.3
        try:
            if isinstance(ts, str):
                dt = _datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, (int, float)):
                dt = _datetime.datetime.fromtimestamp(ts)
            else:
                return 0.3

            now = _datetime.datetime.now(dt.tzinfo)
            delta = now - dt
            hours_ago = delta.total_seconds() / 3600
            days_ago = delta.days

            if hours_ago <= 1:
                return 1.0
            if hours_ago <= 4:
                return 0.95
            if hours_ago <= 12:
                return 0.90
            if hours_ago <= 24:
                return 0.85
            if days_ago <= 1:
                return 0.70
            if days_ago <= 7:
                return 0.50
            if days_ago <= 30:
                return 0.30
            if days_ago <= 90:
                return 0.20
            return 0.10
        except Exception as e:
            logger.debug("memory_manager.time_decay_failed", error=str(e))
            return 0.3

    async def _compute_final_scores(self, query: str, results: list[dict],
                                      config: Any,
                                      query_entities: set[str] | None = None) -> None:
        """统一评分公式: final = 0.5×rerank + 0.3×R + 0.1×kg + 0.1×importance。

        R 为 FSRS-DSR Retrievability（记忆可提取性），替代旧 fluid_score。
        I6: 复用已存储的 entities 字段 + 预提取的 query_entities，
        避免 N+1 次 LLM 调用（原 get_relevance_boost 性能黑洞）。
        """
        if not results:
            return
        # KG 实体匹配加成（复用已提取的 query_entities，避免 N+1 LLM 调用）
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
        # 统一评分公式
        for i, r in enumerate(results):
            # rerank_score: 从 rerank_score 或 rrf_score 字段获取，归一化到 0-1
            rerank_raw = r.get("rerank_score", r.get("rrf_score", 0.0))
            rerank_score = _normalize_score(rerank_raw, default=0.0)
            # R: FSRS-DSR Retrievability（_apply_fsrs_scoring 已计算）
            R = _normalize_score(r.get("fluid_score"), default=0.5)
            # kg_boost: KG 召回标记或实体匹配加成（0.5-1.0），否则 0
            kg_boost_val = kg_boosts[i] if i < len(kg_boosts) else 0.0
            if r.get("kg_recall"):
                # KG 召回候选保底 0.5
                kg_boost_val = max(kg_boost_val, 0.5)
            kg_boost = _normalize_score(kg_boost_val, default=0.0)
            # importance: 记忆重要性
            importance = _normalize_score(r.get("importance"), default=0.5)
            # recency: 时间新鲜度加成（近期记忆优先）
            recency = _normalize_score(self._compute_recency_boost(r), default=0.3)
            # 写入中间分数字段（用于调试和可观测性）
            r["rerank_score"] = rerank_score
            r["fluid_score"] = R
            r["kg_boost"] = kg_boost
            r["importance_score"] = importance
            r["recency_boost"] = recency
            # 统一评分公式: rerank 0.4 + R 0.25 + recency 0.15 + kg 0.1 + importance 0.1
            r["final_score"] = (
                rerank_score * 0.4      # Reranker 精排分数
                + R * 0.25              # FSRS-DSR Retrievability
                + recency * 0.15        # 时间新鲜度加成
                + kg_boost * 0.1        # KG 增强分数
                + importance * 0.1       # 记忆重要性
            )

    async def _apply_topic_trigger(self, query: str, results: list[dict],
                                     k: int,
                                     scope: Any | None = None) -> list[dict]:
        """主动检索 A：话题触发器。

        从 query 抽取 top-N 话题关键词，对每个词做轻量 FTS 检索，
        把"主题相关但未被主路命中"的记忆补充进来，扩大主动联想。
        即使主路 RRF 没召回，话题相关的旧记忆也能浮上来。

        scope 非空时使用 scoped FTS 检索，防止跨用户记忆泄露。
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
                if scope is not None:
                    _topic_hits = await self.memory.search_memories_fts_scoped(
                        _kw, scope=scope, limit=1)
                else:
                    _topic_hits = await self.memory.search_memories_fts(_kw, limit=1)
                for _r in _topic_hits:
                    _rid = str(_r.get("id", ""))
                    if _rid and _rid not in _existing_ids:
                        _existing_ids.add(_rid)
                        # 标记话题触发来源，便于调试和上层 prompt 区分
                        _r["topic_trigger"] = _kw
                        # 话题触发的记忆没有 final_score，用基础分填充避免排序异常
                        # 分数设为 0.25：低于主路 reranker 命中（0.4+），但高于去重阈值，
                        # 让话题触发记忆作为"补充联想"出现在结果末尾，扩大主动联想。
                        _r.setdefault("final_score", 0.25)
                        results.append(_r)
            # 修复：移除函数内部的 [:k] 截断
            # 根因：调用方在调用本函数前已 results = results[:k] 截断（见 retrieve_memories_hybrid L1410），
            # 本函数把 topic_hits append 到末尾后，若再 [:k] 截断，刚 append 的 topic_hits 会全部被丢弃，
            # 导致话题触发器形同虚设（死代码）。
            # 修复后：让 topic_hits 超出 k 的部分保留，由调用方的 _dedup_by_content_similarity 处理后
            # 再统一截断到 k+2（见 retrieve_memories_hybrid L1416 后的截断）。
            logger.debug("memory.topic_trigger",
                         keywords=_topic_keywords,
                         added=sum(1 for r in results if r.get("topic_trigger")))
        except Exception as e:
            logger.debug("memory.topic_trigger_failed", error=str(e))
        return results

    async def _batch_touch_memories(self, mem_ids: list[int | str]) -> None:
        """批量递增记忆访问计数并更新 FSRS 状态（passive_use 信号）。

        检索命中后异步调用，不阻塞检索返回。
        - access_count += 1
        - reinforcement_count += 1（通过 FSRS reinforce）
        - last_review = now
        - 根据 phase 迁移规则更新 phase（21天后 buffer→decay，reinforced 后 stability 增长）

        修复：此前 increment_access_count 从未被调用，记忆永远无法进入 PERMANENT 状态，
        FSRS 遗忘曲线也完全不生效。
        """
        if not mem_ids:
            return
        try:
            now = time.time()
            for mid in mem_ids:
                try:
                    mem = await self.memory.get_memory_by_id(mid)
                    if not mem:
                        continue
                    # 构建 MemoryState
                    created_at = mem.get("created_at", 0.0) or mem.get("timestamp", 0.0)
                    last_review = mem.get("last_review", 0.0) or created_at
                    phase_str = mem.get("phase", "buffer")
                    difficulty = mem.get("difficulty", 5.0)
                    stability = mem.get("stability", S_INIT)
                    rc = mem.get("reinforcement_count", 0)

                    state = MemoryState(
                        difficulty=difficulty,
                        stability=stability,
                        phase=MemoryPhase.safe(phase_str),
                        last_review=last_review,
                        created_at=created_at,
                        reinforcement_count=rc,
                    )
                    # PASSIVE_USE 信号：stability 增长但 growth_factor 较低
                    new_state = self._fsrs.reinforce(state, ReinforcementSignal.PASSIVE_USE, now)

                    await self.memory.update_fsrs_state(
                        mid,
                        difficulty=new_state.difficulty,
                        stability=new_state.stability,
                        phase=new_state.phase.value,
                        last_review=now,
                        reinforcement_count=new_state.reinforcement_count,
                    )
                    # 递增 access_count
                    await self.memory.increment_access_count(mid)
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("memory.touch_failed", mid=mid, error=str(e))
            logger.debug("memory.batch_touched", count=len(mem_ids))
        except Exception as e:
            logger.warning("memory.batch_touch_error", error=str(e))

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
    async def encode_memory(self, context: dict, scope: Any | None = None) -> None:
        """编码记忆（ADD-only 架构）。

        mem0 SPEC 优化：
        1. 写入 is_raw=1 的原始记忆（append-only，不去重，不覆盖）
        2. 异步触发实体提取+链接
        3. 异步触发蒸馏（生成 is_raw=0 的提炼知识，Task 7 实现）

        Args:
            context: 包含 exchanges 列表的上下文
            scope: Scope 对象。None 时使用默认 Scope()。
        """
        # scope 默认值
        if scope is None:
            from memory.scope import Scope
            scope = Scope()

        exchanges = context.get("exchanges", [])
        if not exchanges or len(exchanges) < 2:
            return

        summary = self._generate_summary(exchanges)

        # 安全过滤
        validation = validate_memory_content(summary)
        if validation:
            logger.warning("memory.safety_blocked", reason=validation)
            return

        # ADD-only: 原始记忆不去重，直接写入
        # （_has_duplicate 只在蒸馏时对 is_raw=0 生效，这里不调用）

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

            # ADD-only: 写入 is_raw=1 的原始记忆（不去重，不覆盖）
            mem_id = await self.memory.insert_episodic_memory(
                summary=summary,
                importance=importance,
                emotion_label=emotion,
                scope=scope,
                is_raw=1,
            )

            # Initialize FSRS state for new memory
            now_ts = time.time()
            initial_difficulty = estimate_initial_difficulty(summary, emotion)
            await self.memory.update_fsrs_state(
                mem_id,
                difficulty=initial_difficulty,
                stability=S_INIT,
                phase="buffer",
                last_review=now_ts,
                reinforcement_count=0,
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

            # 双写：同时写入 concept_nodes
            if self.concept_graph and mem_id:
                try:
                    await self.concept_graph.remember(summary, source_mem_id=mem_id)
                except Exception as e:
                    logger.debug("memory.concept_dual_write_failed", error=str(e))

            # ── 父子Chunk: 生成并写入子chunk ──
            import config as _cfg
            if getattr(_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True):
                try:
                    children = self._split_into_children(exchanges, mem_id, summary)
                    if children and self.vec:
                        child_items = []
                        for child in children:
                            child_id = await self.memory.insert_child_chunk(
                                parent_id=mem_id,
                                content=child['content'],
                                embed_content=child['embed_content'],
                                chunk_type=child['chunk_type'],
                                importance=importance * child['weight'],
                                overlap_hash=child['overlap_hash'],
                            )
                            child_items.append((child_id, child['embed_content']))
                        # 批量嵌入子chunk
                        await self.vec.batch_upsert_children(child_items)
                        logger.debug("memory.child_chunks_created",
                                     parent_id=mem_id, count=len(children))
                except Exception as e:
                    logger.debug("memory.child_chunk_failed", error=str(e))

            # ── mem0 SPEC: 异步触发实体提取+链接 ──
            if self.entity_extractor and self.entity_store:
                try:
                    _entity_task = asyncio.create_task(
                        self._extract_and_link_entities(mem_id, summary, scope)
                    )
                    def _log_entity_exception(t: asyncio.Task) -> None:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc:
                            logger.warning("memory.entity_async_failed", error=str(exc))
                    _entity_task.add_done_callback(_log_entity_exception)
                except Exception as e:
                    logger.debug("memory.entity_spawn_failed", error=str(e))

            # ── mem0 SPEC: 异步触发蒸馏（原始记忆 → is_raw=0 提炼知识）──
            full_text_parts = []
            for msg in exchanges[-6:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and content:
                    full_text_parts.append(f"用户说: {content}")
                elif role == "assistant" and content:
                    full_text_parts.append(f"小妲: {content}")
            full_text = "；".join(full_text_parts)[:3000]

            if self.distiller:
                try:
                    _distill_task = asyncio.create_task(
                        self._distill_to_knowledge(
                            mem_id, summary, scope, importance, emotion,
                            full_text=full_text
                        )
                    )
                    def _log_distill_exception(t: asyncio.Task) -> None:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc:
                            logger.warning("memory.distill_async_failed", error=str(exc))
                    _distill_task.add_done_callback(_log_distill_exception)
                except Exception as e:
                    logger.debug("memory.distill_spawn_failed", error=str(e))

            self._last_encode_time = time.time()
            self._pending_encode = False
            logger.info("memory.encoded", summary=summary[:80], importance=importance, is_raw=1)

            # 冷启动路由: 新记忆写入后失效计数缓存, 下次检索立即感知档位变化
            self.invalidate_memory_count_cache()

            if self._query_cache:
                self._query_cache.invalidate()

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

    async def _extract_and_link_entities(self, memory_id: int, summary: str,
                                          scope: Any) -> None:
        """异步提取实体并建立反向链接（mem0 SPEC 优化）。

        Args:
            memory_id: 原始记忆 ID
            summary: 记忆摘要文本
            scope: Scope 对象
        """
        if not self.entity_extractor or not self.entity_store:
            return
        try:
            # 提取实体
            entities = await self.entity_extractor.extract(summary, importance=0.5)
            if not entities:
                return
            # 链接到记忆
            linked = await self.entity_store.link_entities(memory_id, entities, scope=scope)
            logger.debug("memory.entities_linked",
                         memory_id=memory_id, count=linked)
        except Exception as e:
            logger.debug("memory.extract_link_entities_failed", error=str(e))

    async def _entity_recall(self, query: str, scope: Any,
                              recall_limit: int = 50) -> list[dict]:
        """第6路召回：通过实体名反查记忆（mem0 SPEC 优化）。

        流程：
        1. EntityExtractor 规则快抽查询中的实体名（<10ms，不触发 LLM）
        2. EntityStore.recall_by_entities 反查关联记忆

        Args:
            query: 用户查询
            scope: Scope 对象
            recall_limit: 召回上限
        Returns:
            记忆 dict 列表。失败返回空列表（降级）。
        """
        if not self.entity_store or not self.entity_extractor:
            return []
        try:
            entities = self.entity_extractor._rule_based_extract(query)
            if not entities:
                return []
            entity_names = [e.name for e in entities]
            results = await self.entity_store.recall_by_entities(
                entity_names, scope=scope, limit=recall_limit
            )
            for r in results:
                r["entity_recall"] = True
            return results
        except Exception as e:
            logger.debug("memory.entity_recall_failed", error=str(e))
            return []

    async def _apply_entity_boost(self, query: str, candidates: list[dict],
                                   scope: Any) -> list[dict]:
        """精排阶段计算 Entity Boost 并加分（mem0 SPEC 优化）。

        对每个候选记忆，计算其关联实体与查询实体的 boost 值，
        加到 rrf_score 上。

        Args:
            query: 用户查询
            candidates: 候选记忆列表（含 rrf_score）
            scope: Scope 对象
        Returns:
            加分后的候选列表（按 rrf_score 降序）
        """
        if not self.entity_extractor or not self.entity_store:
            return candidates
        if not candidates:
            return candidates
        try:
            query_entities_list = await self.entity_extractor.extract(query, importance=0.3)
            query_entity_names = {e.name for e in query_entities_list}
            if not query_entity_names:
                return candidates

            now = time.time()
            for candidate in candidates:
                mem_id = candidate.get("id")
                if mem_id is None:
                    continue
                boost = await self.entity_store.get_query_entities_boost(
                    mem_id, query_entity_names, now=now
                )
                if boost > 0:
                    candidate["rrf_score"] = candidate.get("rrf_score", 0.0) + boost
                    candidate["entity_boost"] = boost

            candidates.sort(key=lambda x: x.get("rrf_score", 0.0), reverse=True)
            return candidates
        except Exception as e:
            logger.debug("memory.apply_entity_boost_failed", error=str(e))
            return candidates

    async def _hybrid_fts_search_scoped(self, query: str, k: int,
                                         scope: Any, is_raw: int | None) -> list[dict]:
        """FTS 检索 + scope 过滤（mem0 SPEC 优化）"""
        if not self.memory:
            return []
        try:
            return await self.memory.search_memories_fts_scoped(
                query, scope=scope, limit=k * 2, is_raw=is_raw
            )
        except Exception as e:
            logger.warning("memory.fts_scoped_search_failed", error=str(e))
            return []

    async def _distill_to_knowledge(self, raw_id: int, summary: str,
                                     scope: Any, importance: float = 0.5,
                                     emotion: str = "", _retry: int = 0,
                                     full_text: str = "") -> None:
        """将原始记忆蒸馏为提炼知识（允许 UPDATE/DELETE）。

        mem0 SPEC 优化 ADD-only 架构：
        1. 调用 MemoryDistiller 蒸馏
        2. 检查是否已有相似的提炼知识（is_raw=0, 同 scope）
        3a. 有相似 → UPDATE（合并/增强）
        3b. 无相似 → 新建提炼知识（is_raw=0）

        蒸馏失败时异步重试最多 2 次（间隔 30s/60s），避免免费模型超时导致记忆丢失。

        Args:
            raw_id: 原始记忆 ID
            summary: 原始记忆摘要
            scope: Scope 对象
            importance: 重要性
            emotion: 情感标签
            _retry: 当前重试次数（内部使用）
            full_text: 完整对话原文（蒸馏失败时回填用）
        """
        if not self.distiller:
            return
        try:
            try:
                existing = await self.memory.get_memory_by_id(raw_id)
                if existing:
                    ds = existing.get("distill_status", "")
                    if ds == "failed":
                        # 允许重试：清除 failed 状态，重新蒸馏
                        logger.info("memory.distill_retry", raw_id=raw_id)
                        await self.memory.update_distill_status(raw_id, "")
            except Exception as e:
                logger.debug("memory_manager.distill_status_check_failed", error=str(e))
            # 1. 蒸馏（调用已有 MemoryDistiller，传入单条记忆）
            distilled = await self.distiller.distill([{"summary": summary, "timestamp": time.time()}])
            if not distilled or not distilled.strip():
                if _retry < 2:
                    delay = 30 * (_retry + 1)
                    logger.info("memory.distill_empty_retry", raw_id=raw_id,
                               retry=_retry + 1, delay_s=delay)
                    _captured_scope = scope
                    _captured_full_text = full_text

                    async def _retry_distill() -> None:
                        await asyncio.sleep(delay)
                        await self._distill_to_knowledge(
                            raw_id, summary, _captured_scope,
                            importance, emotion, _retry + 1,
                            full_text=_captured_full_text,
                        )

                    try:
                        _bg = asyncio.create_task(_retry_distill())
                        _bg.add_done_callback(_log_task_exception)
                    except RuntimeError:
                        logger.warning("memory.distill_retry_no_loop", raw_id=raw_id)
                else:
                    logger.warning("memory.distill_exhausted_retries", raw_id=raw_id)
                    await self._save_fallback_raw(raw_id, summary, full_text)
                return

            # 2. 检查是否已有相似的提炼知识
            similar = await self._find_similar_knowledge(distilled, scope=scope)

            if similar:
                # 3a. 有相似知识 → UPDATE（合并）
                await self._update_knowledge(similar["id"], distilled, raw_id, scope)
            else:
                # 3b. 无相似知识 → 新建提炼知识（is_raw=0）
                knowledge_id = await self.memory.insert_episodic_memory(
                    summary=distilled,
                    importance=importance,
                    emotion_label=emotion,
                    scope=scope,
                    is_raw=0,
                )
                if self.vec and knowledge_id:
                    try:
                        await self.vec.upsert(knowledge_id, distilled)
                    except Exception as e:
                        logger.debug("memory.distill_vec_upsert_failed", error=str(e))
                logger.info("memory.distilled_new",
                           raw_id=raw_id, knowledge_id=knowledge_id)
            # 蒸馏完成后失效查询缓存：新提炼知识需被后续检索感知
            if self._query_cache:
                self._query_cache.invalidate()
        except Exception as e:
            logger.warning("memory.distill_to_knowledge_failed",
                          raw_id=raw_id, retry=_retry, error=str(e))
            if _retry < 2:
                delay = 30 * (_retry + 1)
                _captured_scope = scope
                _captured_full_text = full_text

                async def _retry_distill_exc() -> None:
                    await asyncio.sleep(delay)
                    await self._distill_to_knowledge(
                        raw_id, summary, _captured_scope,
                        importance, emotion, _retry + 1,
                        full_text=_captured_full_text,
                    )

                try:
                    _bg = asyncio.create_task(_retry_distill_exc())
                    _bg.add_done_callback(_log_task_exception)
                except RuntimeError:
                    logger.warning("memory.distill_retry_no_loop", raw_id=raw_id)
            else:
                await self._save_fallback_raw(raw_id, summary, full_text)

    async def _save_fallback_raw(self, raw_id: int, truncated_summary: str,
                                  full_text: str) -> None:
        try:
            if full_text and len(full_text) > len(truncated_summary):
                await self.memory.update_fallback_raw(raw_id, full_text, "", distill_status="failed")
                logger.info("memory.fallback_raw_updated", raw_id=raw_id,
                           old_len=len(truncated_summary), new_len=len(full_text))
                if self.vec:
                    try:
                        await self.vec.upsert(raw_id, full_text)
                    except Exception as e:
                        logger.debug("memory.fallback_vec_upsert_failed", error=str(e))
            else:
                await self.memory.update_distill_status(raw_id, "distill_failed")
            # summary 更新后失效查询缓存，避免返回旧内容
            if self._query_cache:
                self._query_cache.invalidate()
        except Exception as e:
            logger.warning("memory.fallback_save_failed", raw_id=raw_id, error=str(e))

    async def _find_similar_knowledge(self, summary: str,
                                       scope: Any) -> dict | None:
        """查找相似的提炼知识（is_raw=0, 同 scope）。

        使用 FTS 召回候选 + 字符 bigram Jaccard 相似度阈值过滤，
        避免 FTS 的宽松 token 匹配导致不相关知识被误合并
        （如 "用户喜欢Python" 误匹配 "用户喜欢Java"）。

        Args:
            summary: 待查重的摘要
            scope: Scope 对象
        Returns:
            相似的记忆 dict，或 None
        """
        try:
            candidates = await self.memory.search_memories_fts_scoped(
                summary, scope=scope, limit=5, is_raw=0
            )
            if not candidates:
                return None
            query_bigrams = _char_bigrams(summary)
            if not query_bigrams:
                return None
            for c in candidates:
                candidate_bigrams = _char_bigrams(c.get("summary", ""))
                if not candidate_bigrams:
                    continue
                intersection = query_bigrams & candidate_bigrams
                union = query_bigrams | candidate_bigrams
                jaccard = len(intersection) / len(union)
                if jaccard >= 0.4:
                    return c
            return None
        except Exception as e:
            logger.debug("memory.find_similar_knowledge_failed", error=str(e))
            return None

    async def _update_knowledge(self, knowledge_id: int, new_content: str,
                                 raw_id: int, scope: Any) -> None:
        """更新已有提炼知识（合并新信息）。

        Args:
            knowledge_id: 提炼知识 ID
            new_content: 新蒸馏的内容
            raw_id: 原始记忆 ID（用于溯源）
            scope: Scope 对象
        """
        try:
            # 1. 获取已有知识
            existing = await self.memory.get_memory_by_id(knowledge_id)
            if not existing:
                return

            # 2. LLM 合并新旧知识
            merged = await self.distiller.merge_knowledge(
                existing=existing.get("summary", ""),
                new_content=new_content,
            )

            # 3. 更新记录（version+1，追加 source_raw_ids 溯源链）
            import json
            existing_meta = {}
            try:
                raw_meta = existing.get("metadata_json") or "{}"
                if isinstance(raw_meta, str):
                    existing_meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                existing_meta = {}
            source_raw_ids: list = existing_meta.get("source_raw_ids", [])
            if raw_id not in source_raw_ids:
                source_raw_ids.append(raw_id)
            await self.memory.update_memory_enrichment(
                memory_id=knowledge_id,
                summary=merged,
                metadata_json=json.dumps({
                    "source_raw_ids": source_raw_ids,
                    "merged_at": time.time(),
                }),
            )

            # 4. 向量更新
            if self.vec:
                try:
                    await self.vec.upsert(knowledge_id, merged)
                except Exception as e:
                    logger.debug("memory.update_knowledge_vec_failed", error=str(e))

            logger.info("memory.knowledge_updated",
                       knowledge_id=knowledge_id, raw_id=raw_id)
        except Exception as e:
            logger.warning("memory.update_knowledge_failed", error=str(e))

    def _generate_summary(self, exchanges: list[dict]) -> str:
        parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # Defense-in-depth: strip emotion tags that may have leaked into history
            if content:
                content = re.sub(r'\[emotion:[^\]]*\]', '', content)
                content = re.sub(r'\[\w+/stickers:[^\]]*\]', '', content)
                content = content.strip()
            if role == "user" and content:
                parts.append(f"用户说: {content[:400]}")
            elif role == "assistant" and content:
                # 标记为回复内容，避免被误认为事实性记忆
                parts.append(f"小妲回复: {content[:400]}")

        total_budget = 1500
        joined = "；".join(parts)
        if len(joined) <= total_budget:
            return joined
        kept = []
        remaining = total_budget
        for part in reversed(parts):
            if remaining <= 0:
                break
            if len(part) <= remaining:
                kept.append(part)
                remaining -= len(part) + 1
            else:
                kept.append(part[:remaining])
                remaining = 0
        kept.reverse()
        return "；".join(kept)

    def _split_into_children(self, exchanges: list[dict], parent_id: int,
                             parent_summary: str) -> list[dict]:
        """将对话轮次切分为子chunk，带重叠窗口和 Contextual Retrieval 前缀。

        Returns:
            [{content, embed_content, chunk_type, weight, overlap_hash}, ...]
        """
        import hashlib
        import config as _cfg

        overlap_chars = getattr(_cfg, 'CHILD_CHUNK_OVERLAP_CHARS', 30)
        max_len = getattr(_cfg, 'CHILD_CHUNK_SEGMENT_MAX_LEN', 200)
        max_children = getattr(_cfg, 'CHILD_CHUNK_MAX_PER_PARENT', 10)
        contextual = getattr(_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)

        children: list[dict] = []
        prev_tail = ""

        for msg in exchanges[-8:]:  # 扩大到8轮
            if len(children) >= max_children:
                break
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue

            prefix = "用户说：" if role == "user" else ""
            text = f"{prefix}{content[:max_len]}"

            # 重叠窗口
            overlap_hash = ""
            if prev_tail and overlap_chars > 0:
                overlap = prev_tail[-overlap_chars:]
                overlap_hash = hashlib.sha256(overlap.encode()).hexdigest()[:8]
                text = f"{overlap}…{text}"

            # Contextual Retrieval: 注入父摘要前缀
            if contextual and parent_summary:
                embed_content = f"[上下文: {parent_summary[:80]}] {text}"
            else:
                embed_content = text

            weight = 1.0 if role == 'user' else 0.8

            children.append({
                'content': text,
                'embed_content': embed_content,
                'chunk_type': 'segment',
                'weight': weight,
                'overlap_hash': overlap_hash,
            })

            prev_tail = text

        return children

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
                    lines.append(f"用户: {content[:300]}")
                elif role == "assistant" and content:
                    lines.append(f"{get_agent_display_name('xiaoda')}: {content[:300]}")
            text = "\n".join(lines)
            if not text or len(text) < 10:
                return

            prompt = f"""你是记忆结构化提取助手。从以下对话中提取结构化信息，返回 JSON 格式（只返回 JSON，不要任何其他内容）：

对话内容：
{text}

请返回以下 JSON 格式：
{{
  "summary": "高质量摘要，保留所有关键信息：人物、时间、地点、决策、偏好、情感，200字以内",
  "entities": ["涉及的人物、物品、地点、技术名词等实体"],
  "event_type": "事件类型（对话/决策/偏好/事件/闲聊/调试/学习 之一）",
  "metadata": {{
    "decision": "如果有决策或结论写在这里，没有则空字符串",
    "topic": "主要话题，1-3个词",
    "mood": "用户情绪（喜悦/悲伤/愤怒/平静/焦虑等）"
  }}
}}"""

            messages = [{"role": "user", "content": prompt}]
            result = await self.distiller._call_free_model(messages, temperature=0.3, max_tokens=1024)
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

            # 更新 DB：只用 LLM 提取的 entities/event_type/metadata 补充，不用 LLM 摘要替换原始 summary
            # 原因：原始 summary 是从真实对话直接生成的，保留原始细节；
            #       LLM 摘要是二次加工，可能丢失信息或产生幻觉（用户反馈蒸馏破坏60%+真实内容）
            update_summary = ""
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

            # ── Phase 2: enrichment子chunk化 — 将实体/决策/话题写入子chunk ──
            import config as _enrich_cfg
            if getattr(_enrich_cfg, 'PARENT_CHILD_CHUNK_ENABLED', True) and self.vec:
                try:
                    enrich_parent_summary = update_summary or new_summary or ""
                    enrich_children: list[tuple[int, str]] = []
                    entity_list = data.get("entities", [])
                    meta = data.get("metadata", {})
                    decision = meta.get("decision", "").strip()
                    topic = meta.get("topic", "").strip()

                    # 实体子chunk
                    for ent in entity_list[:5]:  # 最多5个实体
                        ent_str = str(ent).strip()
                        if not ent_str or len(ent_str) < 2:
                            continue
                        content = f"实体: {ent_str}"
                        embed_content = (f"[上下文: {enrich_parent_summary[:80]}] {content}"
                                         if getattr(_enrich_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)
                                         else content)
                        cid = await self.memory.insert_child_chunk(
                            parent_id=mem_id, content=content,
                            embed_content=embed_content, chunk_type='entity',
                            importance=0.7)
                        enrich_children.append((cid, embed_content))

                    # 决策子chunk
                    if decision and len(decision) >= 5:
                        content = f"决策: {decision}"
                        embed_content = (f"[上下文: {enrich_parent_summary[:80]}] {content}"
                                         if getattr(_enrich_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)
                                         else content)
                        cid = await self.memory.insert_child_chunk(
                            parent_id=mem_id, content=content,
                            embed_content=embed_content, chunk_type='decision',
                            importance=0.9)
                        enrich_children.append((cid, embed_content))

                    # 话题子chunk
                    if topic and len(topic) >= 2:
                        content = f"话题: {topic}"
                        embed_content = (f"[上下文: {enrich_parent_summary[:80]}] {content}"
                                         if getattr(_enrich_cfg, 'CONTEXTUAL_RETRIEVAL_ENABLED', True)
                                         else content)
                        cid = await self.memory.insert_child_chunk(
                            parent_id=mem_id, content=content,
                            embed_content=embed_content, chunk_type='topic',
                            importance=0.6)
                        enrich_children.append((cid, embed_content))

                    # 批量嵌入
                    if enrich_children:
                        await self.vec.batch_upsert_children(enrich_children)
                        logger.debug("memory.enrich_child_chunks",
                                     parent_id=mem_id, count=len(enrich_children))
                except Exception as e:
                    logger.debug("memory.enrich_child_failed",
                                 error=str(e), error_type=type(e).__name__)

            # enrichment 更新了 summary/entities/子chunk，失效查询缓存
            if self._query_cache:
                self._query_cache.invalidate()

        except Exception as e:
            logger.debug("memory.enrich_failed",
                         error=str(e), error_type=type(e).__name__)

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
        if not force and not self._pending_encode:
            return
        if not force and now - self._last_message_time < self.IDLE_THRESHOLD:
            return
        if now - self._last_encode_time < self.ENCODE_COOLDOWN:
            return

        self._pending_encode = False
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
                        ent = e.strip()
                        if ent and ent not in seen and len(ent) >= 2:
                            seen.add(ent)
                            tags_set.append(ent)
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

    async def retrieve_comfort_memories(self, limit: int = 2,
                                          scope: Any | None = None) -> list[dict]:
        """主动检索 C：情绪触发 — 检索"安抚性记忆"。

        当检测到用户情绪低落（valence=negative）时，主动检索带正面情绪标签
        的历史记忆（喜悦/happy），作为"安抚素材"注入上下文，让小妲能
        回忆起"曾经让用户开心的事"来温柔陪伴。

        DB 中 emotion_label 列历史数据是中文（喜悦），统一模式后是英文（happy），
        所以两种标签都查，避免漏检。

        Args:
            limit: 返回条数上限（默认 2，避免上下文膨胀）
            scope: Scope 对象。None 时使用默认 Scope()。

        Returns:
            安抚性记忆列表，每条带 emotion_trigger="comfort" 标记
        """
        if scope is None:
            from memory.scope import Scope
            scope = Scope()
        try:
            # 正面情绪标签：中文 + 英文双查
            # 喜悦 = happy；害羞有时也带正面色彩（用户被逗笑），但保守起见只取喜悦
            comfort_labels = ["喜悦", "happy"]
            results = await self.memory.search_memories_by_emotion_scoped(
                comfort_labels, limit=limit, scope=scope
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
        # 定时回忆笔记放在最前——用自然叙述式而非列表
        if recall_notes:
            parts.append("（最近想到的事）")
            for rn in recall_notes:
                text = (rn.get("summary") or "").strip()
                if text:
                    parts.append(text)

        if summaries:
            parts.append("（以前发生过的事）")
            for s in summaries:
                text = (s.get("summary_text") or "").strip()
                if text:
                    parts.append(text)

        if recent:
            if parts:
                parts.append("（最近经历的事）")
            else:
                parts.append("（记得的事）")
            for r in reversed(recent):  # 按时间升序展示
                text = (r.get("summary") or "").strip()
                if text:
                    parts.append(text)

        return "\n".join(parts)

    async def shutdown(self) -> str:
        if self.vec:
            await self.vec.close()
        return "done"