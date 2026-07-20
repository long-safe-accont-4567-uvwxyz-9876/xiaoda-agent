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
from typing import Any
from collections.abc import Callable

from loguru import logger

from memory.fsrs_model import FSRSModel, MemoryState, MemoryPhase


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
    # DB 衍生字段 (仅 consolidate_from_db 使用)
    _db_difficulty: float | None = None
    _db_stability: float | None = None
    _db_phase: str | None = None


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
                  on_consolidate: Callable[[], None] | None = None,
                  memory_db: Any = None) -> None:
        self._memories: dict[str, Memory] = {}
        self._importance_threshold = threshold_importance
        self._strength_threshold = threshold_strength
        self._fsrs = FSRSModel()
        self._scheduler_task: asyncio.Task | None = None
        self._last_consolidate_at = 0
        self._stats = {"consolidated": 0, "decayed": 0, "merged": 0, "strengthened": 0}
        # Dream 整合钩子: 默认联动 L/M/S 心理状态清理 7 天前 M 层数据
        self._on_consolidate = on_consolidate
        # G7: scheduler 用此调 consolidate_from_db 操作真实 DB 记忆
        self._memory_db = memory_db

    def add_memory(self, m: Memory) -> None:
        """添加一条记忆到整合器.

        Args:
            m: 待添加的记忆条目
        """
        self._memories[m.id] = m

    def get_memory(self, mid: str) -> Memory | None:
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

        # 1. 衰减评分: FSRS-DSR Retrievability R = e^(-t/S)
        decayed_ids = []
        for mid, m in list(self._memories.items()):
            state = MemoryState(
                stability=m.strength * 10.0 if m.strength > 0 else 3.0,
                phase=MemoryPhase.REINFORCED,
                last_review=m.last_access,
                created_at=m.created_at,
                reinforcement_count=m.access_count,
            )
            R = state.retrievability(now)
            m.strength = max(m.strength * 0.95, R)
            elapsed_days = (now - m.last_access) / 86400
            m.importance *= math.exp(-elapsed_days * 0.01)
            if (self._fsrs.should_archive(R)
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

    async def consolidate_db(self, memory_db: Any, batch_size: int = 100) -> int:
        """数据库归档 — 遍历活跃记忆, 低 R 归档 (FSRS-DSR)

        统一入口: 遗忘+归档逻辑集中在 DreamConsolidator。
        """
        archived_count = 0
        try:
            memories = await memory_db.get_all_memories(limit=batch_size)
            to_archive: list = []
            now = time.time()
            for mem in memories:
                mem_id = mem.get("id")
                state = MemoryState(
                    difficulty=mem.get("difficulty", 5.0),
                    stability=mem.get("stability", 3.0),
                    phase=MemoryPhase.safe(mem.get("phase", "buffer")),
                    last_review=mem.get("last_review", 0.0) or mem.get("timestamp", 0.0),
                    created_at=mem.get("timestamp", 0.0),
                    reinforcement_count=mem.get("reinforcement_count", 0),
                )
                R = state.retrievability(now)
                if self._fsrs.should_archive(R):
                    to_archive.append(mem_id)
            if to_archive:
                await memory_db.archive_memories_batch(to_archive)
                archived_count = len(to_archive)
            logger.info(f"Dream.consolidate_db archived={archived_count}")
        except Exception as e:
            logger.error(f"Dream.consolidate_db_failed: {e}")
        return archived_count

    async def consolidate_from_db(self, memory_db: Any, batch_size: int = 500) -> dict:
        """★ F5 修复：从DB加载记忆并执行完整4杆框架整合（替代空字典的consolidate）。

        4杆框架：
        1. Decay — Ebbinghaus指数衰减评分
        2. Merge — 同内容前缀聚类，输出相似关系（不删除原始记忆）
        3. Strengthen — 统计高频访问记忆（DB侧无需写入，访问计数在检索时已递增）
        4. Evict — 低分记忆归档（非删除，可恢复）
        """
        t0 = time.time()
        stats = {
            "total": 0,
            "decayed": 0,
            "merged": 0,
            "strengthened": 0,
            "evicted": 0,
            "similar_relationships": [],
        }

        try:
            # 1. 从DB加载活跃记忆（排除已归档）
            rows = await memory_db.get_all_memories(limit=batch_size)
            stats["total"] = len(rows)
            if not rows:
                logger.info("Dream.consolidate_from_db empty, skip")
                return {**stats, "duration_ms": 0.0}

            # 转换DB行 → Memory对象
            memories: dict[str, Memory] = {}
            for row in rows:
                mid = str(row["id"])
                mem = Memory(
                    id=mid,
                    content=row.get("summary", ""),
                    importance=row.get("importance", 0.5),
                    strength=1.0,
                    last_access=row.get("last_review", 0.0) or row.get("timestamp", time.time()),
                    created_at=row.get("created_at", 0.0) or row.get("timestamp", time.time()),
                    access_count=row.get("access_count", 0),
                )
                mem._db_difficulty = row.get("difficulty", 5.0)
                mem._db_stability = row.get("stability", 3.0)
                mem._db_phase = row.get("phase", "reinforced")
                memories[mid] = mem

            now = time.time()

            # 2. Decay — FSRS-DSR Retrievability 衰减评分
            evict_ids: list[str] = []
            for mid, m in memories.items():
                db_difficulty = m._db_difficulty if m._db_difficulty is not None else m.importance * 10.0
                db_stability = m._db_stability if m._db_stability is not None else (m.strength * 10.0 if m.strength > 0 else 3.0)
                db_phase_str = m._db_phase if m._db_phase is not None else 'reinforced'
                try:
                    db_phase = MemoryPhase(db_phase_str)
                except ValueError:
                    db_phase = MemoryPhase.REINFORCED
                state = MemoryState(
                    difficulty=db_difficulty,
                    stability=db_stability,
                    phase=db_phase,
                    last_review=m.last_access,
                    created_at=m.created_at,
                    reinforcement_count=m.access_count,
                )
                R = state.retrievability(now)
                m.strength = max(m.strength * 0.95, R)
                elapsed_days = (now - m.last_access) / 86400
                m.importance *= math.exp(-elapsed_days * 0.01)
                if (self._fsrs.should_archive(R)
                        and m.importance < self._importance_threshold):
                    evict_ids.append(mid)

            # 3. Evict — 低分归档（非删除，可恢复）
            if evict_ids:
                try:
                    await memory_db.archive_memories_batch([int(mid) for mid in evict_ids])
                    stats["evicted"] = len(evict_ids)
                except Exception as e:
                    logger.debug(f"Dream.archive_batch_failed: {e}")
            stats["decayed"] = len(evict_ids)

            # 从内存字典移除已归档的
            for mid in evict_ids:
                memories.pop(mid, None)

            # 4. Merge — 仅报告相似关系。旧 DB 未必有 memory_edges，不能虚构已持久化边，
            # 也不能因前缀相同而物理删除原始证据。
            similar_relationships = self._merge_similar_db(memories)
            stats["similar_relationships"] = similar_relationships
            stats["merged"] = len(similar_relationships)

            # 5. Strengthen — 统计高频访问记忆
            for m in memories.values():
                if m.access_count > 5:
                    stats["strengthened"] += 1

            duration = (time.time() - t0) * 1000
            logger.info(
                f"Dream.consolidate_from_db duration={duration:.1f}ms "
                f"total={stats['total']} decayed={stats['decayed']} "
                f"merged={stats['merged']} strengthened={stats['strengthened']} "
                f"evicted={stats['evicted']}"
            )

            self._stats["consolidated"] += 1
            self._stats["decayed"] += stats["decayed"]
            self._stats["merged"] += stats["merged"]
            self._stats["strengthened"] += stats["strengthened"]
            self._last_consolidate_at = now

            # 同步更新内部状态，确保 stats() 统计准确
            self._memories.update(memories)
            for mid in evict_ids:
                self._memories.pop(mid, None)

            # 联动 L/M/S 心理状态: 清理 7 天前 M 层数据
            self._trigger_mental_state_consolidate()

            return {**stats, "duration_ms": duration}
        except Exception as e:
            logger.error(f"Dream.consolidate_from_db_failed: {e}")
            return stats

    def _merge_similar_db(self, memories: dict[str, Memory]) -> list[dict[str, str]]:
        """按内容前缀发现相似记忆，返回非持久化关系建议。

        每组以 importance 最高的记忆为 source，其余记忆作为 target。调用方可记录或
        下调相似项排序，但不得把返回值当作物理删除列表。
        """
        groups: dict[str, list[str]] = {}
        for mid, m in memories.items():
            # 用前 15 字符作为聚类键（中文15字已足够区分语义，30字太激进会合并无关内容）
            key = m.content[:15].lower() if m.content else ""
            if not key:
                continue
            groups.setdefault(key, []).append(mid)

        relationships: list[dict[str, str]] = []
        for _key, ids in groups.items():
            if len(ids) < 2:
                continue
            ids.sort(key=lambda i: memories[i].importance, reverse=True)
            source_id = ids[0]
            relationships.extend(
                {
                    "source_memory_id": source_id,
                    "target_memory_id": target_id,
                    "edge_type": "similar",
                }
                for target_id in ids[1:]
            )
        return relationships

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
            # 用前 15 字符作为聚类键 (中文15字足够区分语义)
            key = m.content[:15].lower()
            groups.setdefault(key, []).append(mid)

        merged = 0
        for _key, ids in groups.items():
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

    def start_scheduler(self, hour: int = 3) -> asyncio.Task | None:
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
                    # G7: 优先用 consolidate_from_db 操作真实 DB 记忆
                    if self._memory_db is not None:
                        result = await self.consolidate_from_db(self._memory_db)
                        logger.info(
                            f"Dream.scheduler.from_db done "
                            f"archived={result.get('archived', 0) if isinstance(result, dict) else 0}"
                        )
                    else:
                        await self.consolidate()
                        logger.warning("Dream.scheduler.fallback_to_consolidate (no memory_db)")
                except Exception as e:
                    logger.error(f"Dream.scheduler.failed: {e}")

        try:
            loop = asyncio.get_running_loop()
            self._scheduler_task = loop.create_task(_run())
            return self._scheduler_task
        except RuntimeError:
            return None

    async def _run_scheduled_test(self) -> None:
        """测试用: 单次执行 scheduler 整合逻辑 (不循环, 不计算定时).

        G7: 仅供测试调用以验证 scheduler 分支选择正确, 生产环境请用 start_scheduler.
        """
        try:
            if self._memory_db is not None:
                await self.consolidate_from_db(self._memory_db)
            else:
                await self.consolidate()
        except Exception as e:
            logger.error(f"Dream.scheduler_test.failed: {e}")

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
_dream: DreamConsolidator | None = None


def get_dream_consolidator(memory_db: Any = None) -> DreamConsolidator:
    """获取全局 DreamConsolidator 单例, 不存在时创建.

    Args:
        memory_db: MemoryDB 实例, 用于 scheduler 调用 consolidate_from_db.
            首次创建时若提供则注入; 后续调用若已存在单例且原实例未注入 db,
            则后注入 (支持启动早期未初始化 DB 的场景).
    """
    global _dream
    if _dream is None:
        _dream = DreamConsolidator(memory_db=memory_db)
    elif memory_db is not None and _dream._memory_db is None:
        # 后注入 (首次创建时未提供)
        _dream._memory_db = memory_db
    return _dream