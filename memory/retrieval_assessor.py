"""CRAG 检索评估器：评估检索结果质量，低置信度时触发兜底策略

基于 Corrective RAG (arXiv:2401.15884) 的思路，
用轻量级方法评估检索结果与查询的相关性。
"""


class RetrievalAssessor:
    """检索结果质量评估器
    
    基于 Top-3 结果的平均相关性分数评估置信度：
    - 高置信度 (≥0.6): 检索结果可靠
    - 低置信度 (<0.3): 检索结果可能不相关，需要兜底
    - 空结果: 检索失败
    """
    
    HIGH_THRESHOLD = 0.6
    LOW_THRESHOLD = 0.3
    
    def __init__(self):
        self._stats = {
            "total_assessments": 0,
            "high_confidence": 0,
            "low_confidence": 0,
            "empty_results": 0,
        }
    
    def assess(self, query: str, results: list[dict]) -> dict:
        """评估检索结果质量
        
        Returns:
            {
                "confidence": float (0-1),
                "level": "high" | "low" | "empty",
                "should_retry": bool,  # 是否建议重试（扩大候选集）
                "should_fallback": bool,  # 是否建议走 importance fallback
            }
        """
        self._stats["total_assessments"] += 1
        
        if not results:
            self._stats["empty_results"] += 1
            return {
                "confidence": 0.0,
                "level": "empty",
                "should_retry": False,
                "should_fallback": True,
            }
        
        # 取 Top-3 结果的相关性分数
        top3 = results[:3]
        scores = []
        score_source = None  # 记录首个命中的分数键，用于决定是否需要放大
        for item in top3:
            # 按优先级获取分数：rerank_score > rrf_score > effective_score > final_score
            # 使用显式键检查而非 or 链，避免 0.0 分数被 falsy 跳过
            val = None
            for key in ("rerank_score", "rrf_score", "effective_score", "final_score"):
                if key in item and item[key] is not None:
                    val = item[key]
                    if score_source is None:
                        score_source = key
                    break
            try:
                scores.append(float(val) if val is not None else 0.0)
            except (TypeError, ValueError):
                scores.append(0.0)

        avg_score = sum(scores) / len(scores) if scores else 0.0

        # 归一化：rrf_score 通常在 0.01-0.03 范围，需要放大；其他分数已在 0-1 范围
        # 根据分数来源（键名）决定是否放大，而非根据分数大小推断
        if score_source == "rrf_score" and avg_score > 0:
            normalized = min(1.0, avg_score * 30)
        else:
            normalized = avg_score
        
        if normalized >= self.HIGH_THRESHOLD:
            self._stats["high_confidence"] += 1
            return {
                "confidence": normalized,
                "level": "high",
                "should_retry": False,
                "should_fallback": False,
            }
        if normalized >= self.LOW_THRESHOLD:
            return {
                "confidence": normalized,
                "level": "medium",
                "should_retry": False,
                "should_fallback": False,
            }
        self._stats["low_confidence"] += 1
        return {
            "confidence": normalized,
            "level": "low",
            "should_retry": True,
            "should_fallback": False,
        }
    
    @property
    def stats(self) -> dict:
        return self._stats.copy()
