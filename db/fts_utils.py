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
    """提取关键词用于 FTS5 索引和查询，jieba 优先，n-gram 降级。

    CJK 单字补全：jieba 对不在词典中的 CJK 词（如"妲己"）切成单字，
    被 min_length=2 过滤后索引/查询都丢失这些字。修复：CJK 文本在
    jieba 分词后，对被丢弃的单字补入 bigram（2-gram），确保子串可检索。
    例如"妲己传说" → jieba 输出 ["妲","己","传说"] → min_length=2 保留
    ["传说"] → 补入 bigram ["妲己","己传","传说"] → 去重后
    ["传说","妲己","己传"]，搜"妲己"即可命中。
    """
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

    # CJK bigram 补全：对被 min_length 过滤掉的单字，补入相邻字组成的 bigram，
    # 确保不在 jieba 词典中的 CJK 词（如"妲己"）仍可通过 bigram 检索到。
    # 仅在 CJK 模式下补全，非 CJK 文本不需要（英文不存在"字内空格"问题）。
    if has_cjk and min_length >= 2:
        for i in range(len(text) - 1):
            bigram = text[i:i+2]
            if _CJK_RANGE.search(bigram) and bigram not in seen:
                seen.add(bigram)
                result.append(bigram)
        # 单字 CJK 补全：文本仅含单个 CJK 字符时无法生成 bigram，
        # 但仍需存入索引（否则搜"王"永远找不到名为"王"的实体）。
        if len(text) == 1 and _CJK_RANGE.search(text) and text not in seen:
            seen.add(text)
            result.append(text)

    return result


def _build_fts_query(query: str) -> str:
    """构建 FTS5 MATCH 查询字符串，关键词 OR 连接。

    CJK 单字修复：jieba 对某些词切分为单字（如"妲己"→["妲","己"]），
    原 min_length=2 会全部过滤，导致 FTS 查询返回空字符串、搜索静默无结果。
    修复：CJK 查询使用 min_length=1 保留单字 token，并为单字 CJK token
    添加前缀通配符（不加引号，FTS5 对 CJK 前缀匹配需无引号才能生效）。
    例如搜"妲"生成 "妲*"，可匹配以"妲"开头的多字 token（如"妲己"）。
    对于不以该字开头的 token（如搜"己"无法匹配"小妲己"），由调用方
    降级到 LIKE 搜索兜底。
    """
    has_cjk = bool(_CJK_RANGE.search(query))
    min_len = 1 if has_cjk else 2
    tokens = _extract_fts_keywords(query, min_length=min_len)
    quoted = []
    for token in tokens:
        cleaned = _FTS_SPECIAL.sub(" ", token).strip()
        if not cleaned:
            continue
        # CJK 单字 token：不加引号 + 前缀通配符，让 FTS5 能匹配以该字开头的多字 token
        if has_cjk and len(cleaned) == 1 and _CJK_RANGE.search(cleaned):
            quoted.append(f"{cleaned}*")
        else:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted) if quoted else ""
