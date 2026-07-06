"""OntoLearner-inspired ontology complexity scorer.

OntoLearner (arXiv:2607.01977v1) empirical finding:
    "failure modes scale with ontological complexity rather than model size
     or architectural sophistication. The primary bottleneck is not model
     capability, but a structural mismatch between how models encode
     knowledge and how ontologies organize it."

This module scores summary complexity BEFORE KG extraction. High-complexity
summaries are skipped (LLM extraction would produce noise entities per the
paper), saving LLM calls and reducing knowledge graph pollution.

Complexity dimensions (0.0-1.0 each, combined weighted):
- length_score: distance from optimal length band (50-200 chars)
- density_score: token-per-char ratio (high density → ambiguity)
- structural_score: conjunction/clause count (relational complexity)
- lexical_score: abstract/ambiguous term ratio
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from loguru import logger

# "最优"摘要长度区间 — 区间内 length_score=0, 之外按距离衰减
_OPTIMAL_LEN_MIN = 50
_OPTIMAL_LEN_MAX = 200

# 抽象/歧义词集合 — 这些词在 KG 提取中容易产生噪声实体
_ABSTRACT_TERMS = frozenset({
    # 中文抽象词
    "东西", "事情", "那个", "这个", "什么", "怎样", "怎么样", "某种",
    "一些", "许多", "大量", "少量", "差不多", "大概", "可能", "也许",
    "感觉", "觉得", "认为", "想法", "意思", "方面", "问题", "情况",
    "等等", "之类", "什么的", "之类的",  # 开放式枚举 = 实体边界模糊
    # 英文抽象词
    "thing", "stuff", "something", "anything", "everything", "what",
    "some", "many", "much", "might", "maybe", "perhaps", "think", "feel",
    "etc", "etcetera",
})

# 结构复杂度标记 — 多从句/连词/枚举 → 关系复杂
# 注意: `、` 是中文顿号 (枚举分隔符), 是多实体列表的核心标记, 缺失会导致
# 多实体摘要的结构复杂度被严重低估 (OntoLearner: 结构性不匹配是主要瓶颈)
_STRUCTURE_MARKERS = re.compile(
    r"[,，、;；。！？]|并且|或者|但是|然而|因此|所以|因为|虽然|而且|不仅|另外|此外|"
    r"\band\b|\bor\b|\bbut\b|\bhowever\b|\btherefore\b|\bbecause\b",
    re.IGNORECASE,
)


@dataclass
class ComplexityScore:
    """本体复杂度评分结果。"""
    length_score: float = 0.0
    density_score: float = 0.0
    structural_score: float = 0.0
    lexical_score: float = 0.0
    total: float = 0.0
    should_skip: bool = False
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "length": round(self.length_score, 3),
            "density": round(self.density_score, 3),
            "structural": round(self.structural_score, 3),
            "lexical": round(self.lexical_score, 3),
            "total": round(self.total, 3),
            "should_skip": self.should_skip,
        }


def _safe_jieba_cut(text: str) -> list[str]:
    """jieba 分词, 失败时退化为字符级切分。"""
    try:
        import jieba
        return [w for w in jieba.cut(text) if w.strip()]
    except Exception:
        logger.opt(exception=True).debug("ontology_complexity.jieba_fallback")
        return [text[i:i+2] for i in range(0, len(text)-1, 2)]


def score_complexity(summary: str,
                      skip_threshold: float = 0.75,
                      weights: tuple[float, float, float, float] = (0.25, 0.25, 0.30, 0.20),
                      ) -> ComplexityScore:
    """评分摘要的本体复杂度。

    Args:
        summary: 待评分的摘要文本
        skip_threshold: 总分超过此值则 should_skip=True, 跳过 KG 提取
        weights: (length, density, structural, lexical) 权重, 和应为 1.0

    Returns:
        ComplexityScore, total ∈ [0,1], should_skip 表示是否建议跳过 KG 提取
    """
    score = ComplexityScore()
    if not summary or not summary.strip():
        score.should_skip = True  # 空文本跳过
        score.detail = {"reason": "empty"}
        return score

    text = summary.strip()
    char_len = len(text)

    # ── 维度1: 长度分数 ──
    # 区间内 = 0 (最优), 区间外按距离衰减, 超长比超短更严重 (信息过载)
    if char_len < _OPTIMAL_LEN_MIN:
        score.length_score = min(1.0, (_OPTIMAL_LEN_MIN - char_len) / _OPTIMAL_LEN_MIN)
    elif char_len > _OPTIMAL_LEN_MAX:
        # 超长摘要实体过多, KG 提取易产生噪声
        score.length_score = min(1.0, (char_len - _OPTIMAL_LEN_MAX) / _OPTIMAL_LEN_MAX)
    else:
        score.length_score = 0.0

    # ── 维度2: 实体密度分数 ──
    # 词/字符比高 = 信息密集 = 歧义可能性高
    words = _safe_jieba_cut(text)
    word_count = len(words)
    if char_len > 0 and word_count > 0:
        density = word_count / char_len
        # 中文理想密度 ~0.4-0.6 (2-3 字一词), >0.7 或 <0.2 都偏复杂
        if density > 0.7:
            score.density_score = min(1.0, (density - 0.7) / 0.3)
        elif density < 0.2:
            score.density_score = min(1.0, (0.2 - density) / 0.2)
        else:
            score.density_score = 0.0

    # ── 维度3: 结构复杂度 ──
    # 多从句/连词 = 关系复杂, KG 提取易混乱
    markers = _STRUCTURE_MARKERS.findall(text)
    # 4+ 个结构标记 = 高结构复杂度
    score.structural_score = min(1.0, len(markers) / 6.0)

    # ── 维度4: 词汇歧义性 ──
    # 抽象词占比高 = 实体提取易产生噪声
    abstract_hits = sum(1 for w in words if w.lower() in _ABSTRACT_TERMS)
    if word_count > 0:
        score.lexical_score = min(1.0, (abstract_hits / word_count) * 3.0)

    # ── 加权总分 ──
    w_len, w_den, w_str, w_lex = weights
    score.total = (
        w_len * score.length_score
        + w_den * score.density_score
        + w_str * score.structural_score
        + w_lex * score.lexical_score
    )
    score.should_skip = score.total >= skip_threshold
    score.detail = {
        "char_len": char_len,
        "word_count": word_count,
        "markers": len(markers),
        "abstract_hits": abstract_hits,
    }
    return score


# 默认跳过阈值 (可由 config 覆盖)
DEFAULT_SKIP_THRESHOLD = 0.75


def should_extract(summary: str,
                    skip_threshold: float = DEFAULT_SKIP_THRESHOLD,
                    ) -> tuple[bool, ComplexityScore]:
    """决策: 是否对该摘要执行 KG 提取。

    OntoLearner 论文: 失败模式与本体复杂度正相关。
    高复杂度摘要的 LLM 提取易产生噪声实体, 跳过更优。

    Returns:
        (should_extract: bool, score: ComplexityScore)
        should_extract=True 表示可提取, False 表示建议跳过
    """
    score = score_complexity(summary, skip_threshold=skip_threshold)
    return (not score.should_skip), score
