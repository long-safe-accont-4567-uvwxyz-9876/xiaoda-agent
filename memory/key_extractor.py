"""关键词提取器 — jieba 分词 + 停用词 + 同义词归一化

基于 mind v6.2.8 的 key 提取策略，适配中文场景。
"""
import re

import jieba

from loguru import logger

# 停用词表（与项目现有 _TOPIC_STOPWORDS 保持一致 + 扩展）
_STOPWORDS = {
    # 中文停用词
    "的", "是", "了", "在", "和", "与", "或", "也", "都", "就", "还", "又",
    "这", "那", "这个", "那个", "这些", "那些", "什么", "怎么", "为什么",
    "一个", "一些", "一种", "一样", "可以", "能", "能不", "能够", "应该",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
    "自己", "别人", "大家", "咱们", "您",
    "有", "没", "没有", "会", "要", "想", "需要", "必须", "得",
    "把", "被", "让", "使", "给", "对", "跟", "向", "往", "到", "从",
    "上", "下", "里", "外", "前", "后", "左", "右", "中", "间",
    "很", "非常", "太", "更", "最", "比较", "相当", "十分", "极其",
    "不", "别", "勿", "莫", "是否", "是不是", "有没有",
    "着", "过", "地", "得",
    "只", "才", "便", "即", "则", "而", "而且", "并且", "但是", "可是",
    "如果", "虽然", "尽管", "即使", "除非", "因为", "所以", "因此",
    "为了", "至于", "关于", "对于", "根据", "按照", "通过", "经由",
    "由于", "由",
    # 英文停用词
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "shall",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their",
    "this", "that", "these", "those", "what", "which", "who", "whom",
    "and", "or", "but", "not", "no", "nor", "so", "if", "then", "else",
    "when", "where", "why", "how", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "only", "own", "same", "than",
    "too", "very", "just", "now",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "into", "about", "between", "through", "during", "before", "after",
    "up", "down", "out", "off", "over", "under", "again",
}


class KeyExtractor:
    """关键词提取器 — jieba 分词 + 停用词 + 同义词归一化"""

    MAX_KEYS = 24  # 与 mind 一致

    # 同义词归一化映射
    NORMALIZE = {
        "postgre": "postgresql",
        "postgres": "postgresql",
        "redis缓存": "redis",
        "前端": "frontend",
        "后端": "backend",
    }

    def extract(self, text: str, is_query: bool = False) -> list[str]:
        """提取索引关键词

        Args:
            text: 输入文本
            is_query: 是否为查询模式（保留参数，未来可扩展身份 facet key）

        Returns:
            去重后的关键词列表（最多 MAX_KEYS 个）
        """
        if not text or not text.strip():
            return []

        # jieba 分词
        tokens = jieba.lcut(text)

        keys = []
        seen = set()
        for token in tokens:
            token = token.strip()
            if not token or len(token) < 2:
                continue
            # 跳过纯数字
            if token.isdigit():
                continue
            # 跳过纯标点
            if re.match(r'^[\W_]+$', token, re.UNICODE):
                continue
            # 小写化
            lower = token.lower()
            # 停用词过滤
            if lower in _STOPWORDS:
                continue
            # 同义词归一化
            lower = self.NORMALIZE.get(lower, lower)
            # 去重
            if lower in seen:
                continue
            seen.add(lower)
            keys.append(lower)
            if len(keys) >= self.MAX_KEYS:
                break

        return keys
