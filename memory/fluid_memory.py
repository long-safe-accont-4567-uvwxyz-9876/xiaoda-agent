"""流体记忆系统 — 艾宾浩斯遗忘曲线 + 访问强化"""
import math
import time

from loguru import logger


class FluidMemory:
    """流体记忆 — 指数衰减 + 访问强化"""

    LAMBDA_DECAY = 0.05       # 遗忘速率（艾宾浩斯曲线参数）
    ALPHA_BOOST = 0.2         # 访问强化力度
    MAX_BOOST = 0.3           # Boost上限：防止高频访问旧记忆分数超过新记忆
    FORGET_THRESHOLD = 0.05   # 动态遗忘阈值（低于此分数不返回）
    DREAM_THRESHOLD = 0.15    # 梦境归档阈值（低于此分数归档）

    def score(self, similarity: float, created_at: float, access_count: int = 0) -> float:
        """计算综合记忆分数

        公式: score = similarity × e^(-λ × days) + min(α × ln(1 + access_count), MAX_BOOST)

        Boost有上限防止高频访问的旧记忆得分超过新记忆满分1.0。

        Args:
            similarity: 相似度分数 (0~1)
            created_at: 记忆创建时间戳
            access_count: 访问次数

        Returns:
            综合分数 (越高越重要)
        """
        days_passed = (time.time() - created_at) / 86400.0
        decay = math.exp(-self.LAMBDA_DECAY * days_passed)
        boost = min(self.ALPHA_BOOST * math.log(1 + access_count), self.MAX_BOOST)
        return (similarity * decay) + boost

    def should_filter(self, score: float) -> bool:
        """是否应过滤（不返回，不删除）"""
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        """是否应归档（梦境守护）"""
        return score < self.DREAM_THRESHOLD

    # dream() 已迁移到 DreamConsolidator.consolidate_db() (统一遗忘+归档入口)
    # 本模块保留纯评分函数, 供 DreamConsolidator 和其他模块复用
