"""Reciprocal Rank Fusion (RRF) — 多路排序融合的中性底层实现。

memory 与 tool_engine 共用；只依赖标准库，无任何项目内反向依赖，
两个包都可以顶层安全导入（memory→utils、tool_engine→utils 方向均无环）。
"""
from __future__ import annotations


def reciprocal_rank_fusion(ranked_lists: list[list[str]], *, k: int = 60, limit: int = 10,
                           weights: list[float] | None = None,
                           rank_penalty: float = 1.0) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: 多路排序融合算法

    Args:
        ranked_lists: 多路排序结果 (每路是 id 列表, 按相关性降序)
        k: 平滑常数 (标准值 60), 防止排名 1 的项压倒一切
        limit: 返回前 N 个
        weights: 各通道权重 (长度须与 ranked_lists 一致)。
            None 或全等值时退化为等权 RRF (向后兼容)。
            空列表通道不参与融合, 自动置零 (空通道熔断)。
        rank_penalty: 排名惩罚指数 p (RRF rank_penalty)。
            p=1.0 退化为标准 RRF (向后兼容)。
            p>1 放大头部 rank 优势——rank 1 的候选相对 rank N 的得分差距被指数级拉开，
            解决 bge-large 下语义近邻与干扰项 L2 距离极接近 (0.95 vs 1.10)、
            线性 RRF 无法区分导致语义近邻被挤出 top-k 的问题。
    """
    scores: dict[str, float] = {}
    for i, ranked in enumerate(ranked_lists):
        if not ranked:
            continue  # 空通道自动跳过, 不稀释有效候选
        w = weights[i] if weights and i < len(weights) else 1.0
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + w * 1.0 / ((k + rank) ** rank_penalty)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
