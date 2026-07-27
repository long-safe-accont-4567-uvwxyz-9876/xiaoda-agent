"""统一文本相似度工具

rapidfuzz 优先（C 实现，快 100x），difflib 降级（无依赖时）。

设计原则：
- 性能：rapidfuzz 是 C 扩展，O(n*m) 但常数极小，4700 条两两比较 < 1s
- 中文友好：rapidfuzz 基于 Unicode 字符，对中文比 difflib 更准
- 降级安全：rapidfuzz 不可用时自动降级到 difflib，功能不中断
"""
from __future__ import annotations

try:
    from rapidfuzz import fuzz as _fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    from difflib import SequenceMatcher
    _HAS_RAPIDFUZZ = False


def ratio(a: str, b: str) -> float:
    """字符级相似度（0~100）。

    适合：去重判断（"用户喜欢亲密互动" vs "用户偏好亲密互动" = 75）
    不适合：语序变化大的语义匹配（"我喜欢纳西妲" vs "纳西妲是我最喜欢的" = 37.5）
    """
    if not a or not b:
        return 0.0
    if _HAS_RAPIDFUZZ:
        return _fuzz.ratio(a, b)
    return SequenceMatcher(None, a, b).ratio() * 100.0


def partial_ratio(a: str, b: str) -> float:
    """子串匹配相似度（0~100）。

    适合：短文本在长文本中的匹配（用户话语片段 vs 本能文本）
    """
    if not a or not b:
        return 0.0
    if _HAS_RAPIDFUZZ:
        return _fuzz.partial_ratio(a, b)
    # difflib 降级：用较短串在较长串里找最相似片段
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not short:
        return 0.0
    best = 0.0
    for i in range(len(long) - len(short) + 1):
        seg = long[i:i + len(short)]
        r = SequenceMatcher(None, short, seg).ratio()
        if r > best:
            best = r
    return best * 100.0


def token_set_ratio(a: str, b: str) -> float:
    """分词集合相似度（0~100）。

    适合：词序无关的语义匹配（"我喜欢纳西妲" vs "纳西妲我喜欢"）
    对中文：按空格分词，需上层先 jieba 分词后传入
    """
    if not a or not b:
        return 0.0
    if _HAS_RAPIDFUZZ:
        return _fuzz.token_set_ratio(a, b)
    # difflib 降级：用集合 Jaccard 近似
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return ratio(a, b)
    inter = len(ta & tb)
    union = len(ta | tb)
    return (inter / union * 100.0) if union else 0.0


def is_available() -> bool:
    """是否使用 rapidfuzz（True）或 difflib 降级（False）"""
    return _HAS_RAPIDFUZZ
