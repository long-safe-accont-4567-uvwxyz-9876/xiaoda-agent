"""流体记忆系统 — 艾宾浩斯遗忘曲线 + 访问强化"""
from typing import Any
import math
import time
import logging

logger = logging.getLogger(__name__)


class FluidMemory:
    """流体记忆 — 指数衰减 + 访问强化"""

    LAMBDA_DECAY = 0.05       # 遗忘速率（艾宾浩斯曲线参数）
    ALPHA_BOOST = 0.2         # 访问强化力度
    FORGET_THRESHOLD = 0.05   # 动态遗忘阈值（低于此分数不返回）
    DREAM_THRESHOLD = 0.15    # 梦境归档阈值（低于此分数归档）

    def score(self, similarity: float, created_at: float, access_count: int = 0) -> float:
        """计算综合记忆分数

        公式: score = similarity × e^(-λ × days) + α × ln(1 + access_count)

        Args:
            similarity: 相似度分数 (0~1)
            created_at: 记忆创建时间戳
            access_count: 访问次数

        Returns:
            综合分数 (越高越重要)
        """
        days_passed = (time.time() - created_at) / 86400.0
        decay = math.exp(-self.LAMBDA_DECAY * days_passed)
        boost = self.ALPHA_BOOST * math.log(1 + access_count)
        return (similarity * decay) + boost

    def should_filter(self, score: float) -> bool:
        """是否应过滤（不返回，不删除）"""
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        """是否应归档（梦境守护）"""
        return score < self.DREAM_THRESHOLD

    async def dream(self, memory_db: Any, batch_size: int = 100) -> int:
        """梦境归档 — 遍历活跃记忆，低分归档

        Args:
            memory_db: MemoryDB 实例
            batch_size: 每批处理数量

        Returns:
            归档的记忆数量
        """
        archived_count = 0
        try:
            # 获取所有活跃记忆
            memories = await memory_db.get_all_memories(limit=batch_size)
            for mem in memories:
                mem_id = mem.get("id")
                created_at = mem.get("timestamp", time.time())
                access_count = mem.get("access_count", 0)
                # 使用中等相似度评估（归档不依赖查询）
                s = self.score(similarity=0.5, created_at=created_at, access_count=access_count)
                if self.should_archive(s):
                    await memory_db.archive_memory(mem_id)
                    archived_count += 1
            logger.info("fluid_memory.dream_completed", extra={"archived": archived_count})
        except Exception as e:
            logger.error(f"fluid_memory.dream_failed: {e}")
        return archived_count
