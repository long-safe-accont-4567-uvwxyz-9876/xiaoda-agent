"""Dream Consolidation (A5) — 夜周期 Ebbinghaus 衰减

参考:
- Sleep Baby Sleep: Memory Consolidation in LLM Agents
- Ebbinghaus Forgetting Curve
- Complementary Learning Systems (CLS) theory

特性:
- 夜周期任务: 每天 03:00 执行
- Ebbinghaus 衰减: R = e^(-t/S), S=memory strength
- 重要性重评估: 旧记忆按相关性衰减
- 梦境合并: 相似记忆片段合并为更通用模式
- 强化高频使用模式
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger


@dataclass
class Memory:
    """记忆条目"""
    id: str
    content: str
    importance: float = 0.5       # 0-1
    strength: float = 1.0        # 衰减强度
    last_access: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    access_count: int = 0
    decay_rate: float = 0.1      # 越大衰减越快


class DreamConsolidator:
    """梦境整合器

    用法:
        dream = DreamConsolidator()
        # 注入记忆源
        dream.add_memory(Memory(id="m1", content="...", importance=0.7))
        # 触发夜周期
        await dream.consolidate()
        # 启动定时任务
        dream.start_scheduler()
    """

    def __init__(self, threshold_importance: float = 0.2,
                  threshold_strength: float = 0.1,
                  on_consolidate: Optional["Callable[[], None]"] = None) -> None:
        self._memories: dict[str, Memory] = {}
        self._importance_threshold = threshold_importance
        self._strength_threshold = threshold_strength
        self._scheduler_task: Optional[asyncio.Task] = None
        self._last_consolidate_at = 0
        self._stats = {"consolidated": 0, "decayed": 0, "merged": 0, "strengthened": 0}
        # Dream 整合钩子: 默认联动 L/M/S 心理状态清理 7 天前 M 层数据
        self._on_consolidate = on_consolidate

    def add_memory(self, m: Memory) -> None:
        """添加一条记忆到整合器.

        Args:
            m: 待添加的记忆条目
        """
        self._memories[m.id] = m

    def get_memory(self, mid: str) -> Optional[Memory]:
        """按 ID 获取记忆, 不存在返回 None.

        Args:
            mid: 记忆 ID

        Returns:
            记忆条目或 None
        """
        return self._memories.get(mid)

    def access(self, mid: str) -> None:
        """访问记忆, 强化强度"""
        m = self._memories.get(mid)
        if not m:
            return
        m.access_count += 1
        m.last_access = time.time()
        # 重复访问增强强度
        m.strength = min(1.0, m.strength + 0.05)
        # 衰减率降低 (越用越不容易忘)
        m.decay_rate = max(0.01, m.decay_rate * 0.95)

    async def consolidate(self) -> dict:
        """执行一次整合

        1. 应用 Ebbinghaus 衰减
        2. 删除低于阈值的记忆
        3. 合并相似记忆
        4. 强化高频访问记忆
        """
        t0 = time.time()
        now = time.time()

        # 1. Ebbinghaus 衰减: R = e^(-t/S), S = 1/strength
        decayed_ids = []
        for mid, m in list(self._memories.items()):
            elapsed_days = (now - m.last_access) / 86400
            retention = math.exp(-elapsed_days * m.decay_rate / max(0.1, m.strength))
            m.strength = retention
            # 重要性也随时间衰减 (但更慢)
            m.importance *= math.exp(-elapsed_days * 0.01)
            if (m.strength < self._strength_threshold
                    and m.importance < self._importance_threshold):
                decayed_ids.append(mid)

        for mid in decayed_ids:
            self._memories.pop(mid, None)
        self._stats["decayed"] += len(decayed_ids)

        # 2. 合并相似记忆 (简单实现: 按 content 前 N 字符聚类)
        merged_count = self._merge_similar()

        # 3. 强化高频访问
        strengthened = 0
        for m in self._memories.values():
            if m.access_count > 5:
                m.strength = min(1.0, m.strength + 0.1)
                m.importance = min(1.0, m.importance + 0.05)
                strengthened += 1
        self._stats["strengthened"] += strengthened

        self._stats["consolidated"] += 1
        self._last_consolidate_at = now

        duration = (time.time() - t0) * 1000
        logger.info(f"Dream.consolidate done duration={duration:.1f}ms "
                     f"decayed={len(decayed_ids)} merged={merged_count} "
                     f"strengthened={strengthened} total={len(self._memories)}")

        # 联动 L/M/S 心理状态: 清理 7 天前 M 层数据
        self._trigger_mental_state_consolidate()

        return {
            "duration_ms": duration,
            "decayed": len(decayed_ids),
            "merged": merged_count,
            "strengthened": strengthened,
            "total_remaining": len(self._memories),
        }

    def _trigger_mental_state_consolidate(self) -> None:
        """触发 L/M/S 心理状态 Dream 整合 (清理 7 天前 M 层数据).

        优先调用自定义 on_consolidate 钩子; 否则调用已初始化的全局 MentalStateManager 单例
        (未初始化时跳过, 避免在测试环境创建副作用).
        任何异常都被吞掉, 不影响 Dream 主流程.
        """
        try:
            if self._on_consolidate is not None:
                self._on_consolidate()
            else:
                from core.mental_state import get_mental_state_manager_if_exists
                mgr = get_mental_state_manager_if_exists()
                if mgr is not None:
                    mgr.consolidate_dream()
        except Exception as e:
            logger.debug(f"Dream.mental_state_consolidate_failed: {e}")

    def _merge_similar(self) -> int:
        """合并相似记忆 (基于内容前缀聚类)"""
        groups: dict[str, list[str]] = {}
        for mid, m in self._memories.items():
            # 用前 30 字符作为聚类键 (短前缀便于合并相似条目)
            key = m.content[:30].lower()
            groups.setdefault(key, []).append(mid)

        merged = 0
        for key, ids in groups.items():
            if len(ids) < 2:
                continue
            # 选 importance 最高的作为主记忆
            ids.sort(key=lambda i: self._memories[i].importance, reverse=True)
            master_id = ids[0]
            master = self._memories[master_id]
            # 合并其他记忆的强度和访问次数
            for sid in ids[1:]:
                s = self._memories.pop(sid)
                master.strength = min(1.0, master.strength + s.strength * 0.5)
                master.access_count += s.access_count
                master.importance = max(master.importance, s.importance)
                merged += 1
        self._stats["merged"] += merged
        return merged

    def start_scheduler(self, hour: int = 3) -> Optional[asyncio.Task]:
        """启动定时任务: 每天 hour 点执行"""
        async def _run() -> None:
            while True:
                # 计算到下一个 hour 点的秒数
                now = time.localtime()
                target = time.mktime(time.struct_time((
                    now.tm_year, now.tm_mon, now.tm_mday,
                    hour, 0, 0, 0, 0, -1
                )))
                if target <= time.time():
                    target += 86400  # 明天
                wait = target - time.time()
                logger.info(f"Dream.scheduler next_run_in={wait:.0f}s")
                await asyncio.sleep(wait)
                try:
                    await self.consolidate()
                except Exception as e:
                    logger.error(f"Dream.scheduler.failed: {e}")

        try:
            loop = asyncio.get_event_loop()
            self._scheduler_task = loop.create_task(_run())
            return self._scheduler_task
        except RuntimeError:
            return None

    def stop_scheduler(self) -> None:
        """取消定时整合任务, 释放后台协程."""
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None

    def stats(self) -> dict:
        """返回整合统计 (含记忆总数/平均强度/平均重要性)."""
        return {
            **self._stats,
            "total_memories": len(self._memories),
            "last_consolidate_at": self._last_consolidate_at,
            "avg_strength": (sum(m.strength for m in self._memories.values())
                                / max(1, len(self._memories))),
            "avg_importance": (sum(m.importance for m in self._memories.values())
                                  / max(1, len(self._memories))),
        }


# 全局单例
_dream: Optional[DreamConsolidator] = None


def get_dream_consolidator() -> DreamConsolidator:
    """获取全局 DreamConsolidator 单例, 不存在时创建."""
    global _dream
    if _dream is None:
        _dream = DreamConsolidator()
    return _dream
