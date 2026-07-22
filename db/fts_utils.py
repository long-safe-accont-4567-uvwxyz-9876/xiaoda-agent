"""FTS5 全文检索分词工具 —— 从 memory/memory_manager.py 抽取.

原 memory.memory_manager 中的 _tokenize_for_fts / _extract_fts_keywords /
_build_fts_query 被 db.database / db.db_memory 反向导入, 形成循环:

    db.database -> memory.memory_manager -> db.database
    db.db_memory -> memory.memory_manager -> db.db_memory

将这三个纯函数及其依赖的正则常量抽到独立模块 db.fts_utils, 该模块不依赖任何
项目内模块 (仅依赖标准库 re 与可选的 jieba), 从而打破 db <-> memory 的循环.
"""
from __future__ import annotations

import re

# ── FTS5 预分词相关正则 ──
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
        tok = token.strip()
        if len(tok) >= min_length and tok not in seen:
            seen.add(tok)
            result.append(tok)
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
