"""流体记忆系统 — mind 风格 Ebbinghaus 增量模型

R = e^(-t/S), S = 3 + 14×access_count
score = similarity × peak_weight × retention
"""
import math
import time

from loguru import logger


class FluidMemory:
    """流体记忆 — Ebbinghaus 增量式稳定性模型

    与旧公式的区别：
    - 旧: similarity × e^(-λ×days) + min(α×ln(1+access), 0.3)
    - 新: similarity × peak_weight × e^(-days / (3 + 14×access))
    - 核心变化：确认次数影响稳定性（半衰期），而非加法 boost
    - 效果：10次确认的记忆 30 天后保留率 81%，远超旧的 ~30%
    """

    # 新参数（与 mind 一致）
    STABILITY_BASE_DAYS = 3.0       # 未确认记忆 3 天半衰期
    STABILITY_PER_ACCESS = 14.0     # 每次确认买 14 天稳定性
    BOOST_PER_ACCESS = 0.15        # 每次确认权重增量（ConceptGraph 使用）
    GRACE_DAYS = 21                # 新记忆 21 天缓冲期（艾宾浩斯：近期记忆不衰减）
    PERMANENT_ACCESS_THRESHOLD = 5 # 确认次数 ≥ 此值 → 永久记忆（不衰减）
    WEIGHT_THRESHOLD = 0.1         # 修剪阈值
    FORGET_THRESHOLD = 0.05   # 动态遗忘阈值（低于此分数不返回）
    DREAM_THRESHOLD = 0.15    # 梦境归档阈值（低于此分数归档）

    def score(self, similarity: float, created_at: float,
              access_count: int = 0, peak_weight: float = 1.0) -> float:
        """计算综合记忆分数（艾宾浩斯遗忘曲线 + 缓冲期 + 永久记忆）

        三层保护：
        1. 永久记忆：access_count ≥ PERMANENT_ACCESS_THRESHOLD → retention=1.0，永不衰减
        2. 缓冲期：创建 ≤ 21 天 → retention=1.0，近期记忆不衰减
        3. 正常衰减：R = e^(-days / stability)，stability = 3 + 14×access_count

        Args:
            similarity: 相似度分数 (0~1)
            created_at: 记忆创建时间戳
            access_count: 确认/访问次数
            peak_weight: 历史最高权重（默认 1.0）

        Returns:
            综合分数 (越高越重要)
        """
        days = max(0, (time.time() - created_at) / 86400.0)

        # 永久记忆：反复被提及 → 不衰减
        if access_count >= self.PERMANENT_ACCESS_THRESHOLD:
            retention = 1.0
        # 缓冲期：新记忆 21 天内不衰减（给记忆"存活"的机会）
        elif days <= self.GRACE_DAYS:
            retention = 1.0
        # 正常艾宾浩斯衰减
        else:
            stability = self.STABILITY_BASE_DAYS + access_count * self.STABILITY_PER_ACCESS
            retention = math.exp(-days / stability)

        weight = peak_weight * retention
        return similarity * weight

    def is_permanent(self, access_count: int) -> bool:
        """判断是否已成为永久记忆（确认次数足够多）"""
        return access_count >= self.PERMANENT_ACCESS_THRESHOLD

    def should_filter(self, score: float) -> bool:
        """是否应过滤（不返回，不删除）"""
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        """是否应归档（梦境守护）"""
        return score < self.DREAM_THRESHOLD

    # dream() 已迁移到 DreamConsolidator.consolidate_db() (统一遗忘+归档入口)
    # 本模块保留纯评分函数, 供 DreamConsolidator 和其他模块复用
