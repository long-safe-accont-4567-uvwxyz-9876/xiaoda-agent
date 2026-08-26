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

# CJK 停用词：高频无意义单字，在 FTS 查询中匹配几乎所有记忆，纯噪声。
# 与 FTS_DROP_CJK_SINGLE（丢弃所有单字）不同，停用词只过滤已知高频字，
# 保留有区分度的单字（如"叫""吃""写""喝"），避免语义改写查询的 FTS 兜底被摧毁。
# 诊断：FTS_DROP_CJK_SINGLE=True 时 Recall 78.1%→25.0%，根因是"我的联系方式是多少"
# 中"我"被删后 FTS 无命中；停用词方案保留"联""系"等有意义单字。
_CJK_STOP_WORDS: frozenset[str] = frozenset({
    "我", "你", "他", "她", "它", "们",
    "的", "了", "在", "是", "有", "和", "与", "或", "不", "没",
    "也", "都", "就", "要", "会", "能", "可", "以",
    "这", "那", "什", "么", "怎", "哪", "谁", "多", "少",
    "吗", "呢", "啊", "吧", "呀", "啦", "哦", "哈",
    "一", "个", "上", "下", "中", "里", "到", "着", "过",
    "很", "好", "还", "又", "再", "把", "被", "让", "给",
    "从", "对", "比", "更", "最", "太", "真", "才", "已",
})


def _tokenize_for_fts(text: str) -> str:
    """将文本分词后用空格连接，用于 FTS5 预分词存储"""
    return " ".join(_extract_fts_keywords(text))


def _extract_fts_keywords(text: str, *, min_length: int = 2) -> list[str]:
    """提取关键词用于 FTS5 索引和查询，jieba 优先，n-gram 降级

    CJK 单字也保留（"你是谁"→你/是/谁）：索引与查询两侧同规则，
    否则纯单字短查询（你是谁/陪着我）分词后全部被过滤，FTS 短路返回空。
    英文/数字 token 仍按 min_length=2 过滤，避免 "a"/"to" 等高频词噪音。
    """
    has_cjk = bool(_CJK_RANGE.search(text))
    if has_cjk:
        try:
            import jieba
            raw_tokens = jieba.lcut_for_search(text)
        except ImportError:
            # n-gram 降级（含单字 gram，与 jieba 路径的单字保留保持一致）
            raw_tokens = [text[i:i+n] for n in range(1, 5) for i in range(len(text)-n+1)]
    else:
        raw_tokens = _KEYWORD_SPLIT.split(text.lower())

    seen = set()
    result = []
    for token in raw_tokens:
        tok = token.strip()
        if not tok:
            continue
        # 纯 CJK 单字（如 "你"）放行；其余 token 仍需 min_length
        effective_min = 1 if (len(tok) == 1 and _CJK_RANGE.fullmatch(tok)) else min_length
        if len(tok) >= effective_min and tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


def _build_fts_query(query: str, *, drop_cjk_single: bool = False,
                     filter_stop_words: bool = False) -> str:
    """构建 FTS5 MATCH 查询字符串，关键词 OR 连接。

    drop_cjk_single=True 时：仅当提取出 len>=2 的 CJK token（多字词）时，
    丢弃所有 CJK 单字 token（"我/最/近/在/写"等高频字）。
    解决长查询被单字 OR 淹没的问题——如"我最近在写什么后端代码"现状会拆出
    单字"我"匹配几乎所有记忆（每条以"我"开头），召回 10+ 条无关记忆稀释向量信号。
    短查询（如"你是谁"）无多字词，仍保留单字兜底，不影响命中。

    filter_stop_words=True 时：过滤 CJK 停用词（_CJK_STOP_WORDS），
    比 drop_cjk_single 更精准——只删"我/的/了/是"等高频无意义单字，
    保留"叫/吃/写/喝"等有区分度的单字，避免语义改写查询的 FTS 兜底被摧毁。
    两种模式可叠加：drop_cjk_single 先执行（删所有单字），filter_stop_words 后执行。
    """
    tokens = _extract_fts_keywords(query)
    if drop_cjk_single:
        multi = [t for t in tokens if len(t) >= 2]
        if multi:
            tokens = multi
    if filter_stop_words:
        filtered = [t for t in tokens if not (len(t) == 1 and t in _CJK_STOP_WORDS)]
        if filtered:
            tokens = filtered
    quoted = []
    for token in tokens:
        cleaned = _FTS_SPECIAL.sub(" ", token).strip()
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted) if quoted else ""
