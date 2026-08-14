"""memory_manager 的模块级纯函数与工具类（从 memory_manager.py 抽取而来）。

memory_manager.py 通过 `from memory._memory_utils import ...` 重新导出这些名称，
以保持 `from memory.memory_manager import X` 的向后兼容。
"""
from typing import ClassVar
import asyncio
import re
import time
from loguru import logger

from config import get_agent_display_name


def _stage_log(stage: str, t0: float, query: str = "") -> None:
    """记录检索子阶段耗时（诊断阻塞根因用）。

    日志剥离结构化字段时，用 INFO 单条消息也能看到每个子阶段耗时，
    便于定位记忆检索 5s 超时里到底哪一步（embed/rerank/DB）最慢。
    """
    _ms = int((time.time() - t0) * 1000)
    # 仅记录 >50ms 的阶段，避免刷屏；>1000ms 用 WARNING 突出
    if _ms > 1000:
        logger.warning("memory.retrieve_stage_cost stage={} elapsed_ms={} query={}",
                       stage, _ms, (query or "")[:40])
    elif _ms > 50:
        logger.info("memory.retrieve_stage_cost stage={} elapsed_ms={} query={}",
                    stage, _ms, (query or "")[:40])


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


# 中文数字 → 阿拉伯数字映射（用于日期解析）
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num_to_int(text: str) -> int | None:
    """将中文数字（1-31，支持月和日）转为 int。无法解析返回 None。

    支持：一/二/.../九/十/十一/.../十九/二十/.../二十九/三十/三十一
    """
    text = text.strip()
    if not text:
        return None
    # 纯阿拉伯数字
    if text.isdigit():
        return int(text)
    # 单字：一~十
    if text in _CN_DIGIT:
        return _CN_DIGIT[text]
    # "十X"：十一=11, 十二=12, ... 十九=19
    if len(text) == 2 and text[0] == "十" and text[1] in _CN_DIGIT:
        return 10 + _CN_DIGIT[text[1]]
    # "X十"：二十=20, 三十=30
    if len(text) == 2 and text[1] == "十" and text[0] in _CN_DIGIT:
        return _CN_DIGIT[text[0]] * 10
    # "X十Y"：二十一=21, 三十一=31
    if len(text) == 3 and text[1] == "十" and text[0] in _CN_DIGIT and text[2] in _CN_DIGIT:
        return _CN_DIGIT[text[0]] * 10 + _CN_DIGIT[text[2]]
    return None


# 匹配中文/阿拉伯数字的月日：七月十八号 / 7月18号 / 十二月三十一日
_CN_DATE_PATTERN = re.compile(
    r"([一二三四五六七八九十两\d]{1,3})\s*月\s*([一二三四五六七八九十两\d]{1,3})\s*[号日]"
)


def _parse_temporal_query(query: str) -> tuple[float, float] | None:
    """从用户查询中解析时间词，返回 [start_ts, end_ts] 时间戳区间（秒）。

    支持的格式：
    1. 相对时间词：刚才/刚刚/N小时前/N分钟前/昨天/前天/大前天/今天/上周/上个月/前几天/最近
    2. 绝对日期：7月15号/7月15日/12月1号/七月十八号/十二月三十一号
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

    # 匹配 "N月N号"/"N月N日"（支持中文数字和阿拉伯数字）/ "N.N日"/"N.N号"
    # 修复多日期解析 bug：原 re.search 只匹配第一个日期，
    # 导致"7月18号、19号、20号、21号、22号"只查7月18号一天 → 小妲"想不起来"
    # 修复中文数字 bug：原正则只匹配阿拉伯数字，"七月十八号"完全匹配不到 → 不走时间检索
    # 修复省略月份 bug：中文表达"七月十八号、十九号、二十号"后续日期省略月份，需补全
    # 现在用 finditer 收集所有日期，多日期时返回最早到最晚的合并范围
    all_date_matches = list(_CN_DATE_PATTERN.finditer(query))
    # 兼容 "7.16号" 格式
    _DATE_PATTERN_DOT = re.compile(r"(\d{1,2})\.(\d{1,2})\s*[号日]")
    all_date_matches += list(_DATE_PATTERN_DOT.finditer(query))
    # CodeRabbit 复审修复：按源位置排序，确保 last_month/last_match_end 代表最后的文本日期
    # 避免混合格式（如"7.16号、7月18号"）时 last_match_end 指向中间位置导致纯日扫描重复
    all_date_matches.sort(key=lambda _m: _m.start())

    # 解析完整日期（X月Y号），并记录最后一个月份用于补全省略月份的日期
    parsed_dates: list[_datetime.datetime] = []
    last_month: int | None = None  # 跟踪最后出现的月份，用于"十九号、二十号"省略月份的情况
    last_match_end: int = 0  # 上一个日期匹配的结束位置

    for dm in all_date_matches:
        m = _cn_num_to_int(dm.group(1))
        d = _cn_num_to_int(dm.group(2))
        if m is None or d is None:
            continue
        if not (1 <= m <= 12 and 1 <= d <= 31):
            continue
        y = now.year
        if m > now.month or (m == now.month and d > now.day):
            y = now.year - 1
        try:
            parsed_dates.append(_datetime.datetime(y, m, d, tzinfo=now.tzinfo))
        except ValueError:
            continue
        last_month = m
        last_match_end = dm.end()

    # 扫描省略月份的纯日："十九号" / "20号" / "二十一号"
    # 仅当已出现过完整日期（last_month 不为 None）时才扫描
    # 用分隔符 [、,，到~—-] 分隔，取每个分段中的 "X号/X日"
    if last_month is not None:
        # 匹配纯日号：十九号 / 20号 / 二十一号（不带"月"或"."前缀）
        # CodeRabbit 复审修复：加强 _DAY_ONLY_PATTERN，拒绝完整月日表达式中的纯日匹配
        # (?<!月) 拒绝"7月18号"中的"18号"（前一个字符是"月"）
        # (?<!\.) 拒绝"7.16号"中的"16号"（前一个字符是"."）
        _DAY_ONLY_PATTERN = re.compile(
            r"(?<!月)(?<!\.)([一二三四五六七八九十两\d]{1,3})\s*[号日]"
        )
        # 从最后一个完整日期匹配位置之后扫描
        search_text = query[last_match_end:] if last_match_end else query
        for dm in _DAY_ONLY_PATTERN.finditer(search_text):
            d = _cn_num_to_int(dm.group(1))
            if d is None or not (1 <= d <= 31):
                continue
            y = now.year
            if last_month > now.month or (last_month == now.month and d > now.day):
                y = now.year - 1
            try:
                candidate = _datetime.datetime(y, last_month, d, tzinfo=now.tzinfo)
                # 避免重复（与已解析日期相同）
                if candidate not in parsed_dates:
                    parsed_dates.append(candidate)
            except ValueError:
                continue

    if parsed_dates:
        # 单日期：保持原有精确逻辑（小时范围/时段/整天）
        if len(parsed_dates) == 1:
            base_date = parsed_dates[0]
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

        # 多日期：返回最早日期 0:00 到最晚日期 23:59 的合并范围
        # 这样能覆盖用户提到的所有日期（如"7月18号到22号"或"7月18号、19号、20号"）
        parsed_dates.sort()
        start = parsed_dates[0].replace(hour=0, minute=0, second=0, microsecond=0)
        end = parsed_dates[-1].replace(hour=23, minute=59, second=59, microsecond=0)
        logger.info("memory.temporal_multi_date",
                    count=len(parsed_dates),
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"))
        return start.timestamp(), end.timestamp()

    # 没有匹配到绝对日期，继续走相对日期逻辑（昨天/今天/上周等）

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
