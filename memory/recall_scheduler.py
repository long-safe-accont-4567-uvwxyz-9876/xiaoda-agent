"""MemoryRecallScheduler — 主动检索 B：定时回忆任务调度器。

独立 asyncio 后台循环，不依赖用户消息触发（区别于 BackgroundTaskManager 的对话驱动模式）。
每 5 分钟 tick 一次，检查 cron_last_run 表，距上次回忆 >= RECALL_INTERVAL_HOURS 小时则触发：

  1. 从 episodic_memories 取最近 hours_back 小时内 importance >= min_importance 的记忆
  2. 调 MemoryDistiller.distill_recall 整理成"回忆笔记"（叙事风格）
  3. 写入 memory_recall_notes 表
  4. 后续 build_memory_prompt / retrieve_memories 可主动拉取这些笔记

调度策略：
- 默认每 3 小时一次（hours_back=3h，正好覆盖上一次回忆以来的全部新记忆）
- 凌晨 0-7 点跳过（DND，避免夜间频繁触发 LLM 调用）
- 每次失败会写 cron_last_run，避免短时间内反复重试

挂载点：web/server.py 的 _start_services()，仿 GreetingScheduler 模式。
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from agent_core.core import AgentCore


def _get_local_now() -> datetime:
    """获取本地时间（使用显式时区，修复 Windows/Docker 中系统时区不正确的问题）。"""
    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(tz)


class MemoryRecallScheduler:
    """定时回忆任务调度器。"""

    TICK_SECONDS = 300  # 每 5 分钟检查一次
    RECALL_INTERVAL_HOURS = 3.0      # 默认每 3 小时回忆一次
    HOURS_BACK = 3.0                 # 回顾窗口：最近 3 小时
    MIN_IMPORTANCE = 0.6             # 重要性下限
    MIN_MEMORIES = 3                 # 少于 3 条不触发
    DND_START_HOUR = 0               # 凌晨免打扰起点（含）
    DND_END_HOUR = 7                 # 凌晨免打扰终点（不含）

    CRON_TASK_NAME = "memory_recall"

    def __init__(self, core: AgentCore) -> None:
        self.core = core
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("memory_recall_scheduler.started",
                        interval_hours=self.RECALL_INTERVAL_HOURS)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            logger.info("memory_recall_scheduler.stopped")

    async def _loop(self) -> None:
        """主循环：每 TICK_SECONDS 秒检查一次是否该触发回忆。"""
        # 启动后等 60s 再首次检查，避免与服务启动初期资源争抢
        await asyncio.sleep(60)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("memory_recall_scheduler.tick_error", error=str(e))
            await asyncio.sleep(self.TICK_SECONDS)

    def _is_dnd(self) -> bool:
        """凌晨 DND 时段（0:00-7:00）跳过回忆任务。"""
        hour = _get_local_now().hour
        return self.DND_START_HOUR <= hour < self.DND_END_HOUR

    async def _should_run(self) -> bool:
        """检查距上次回忆是否已超过间隔。"""
        try:
            last_run = await self.core.db.get_cron_last_run(self.CRON_TASK_NAME)
            if last_run is None:
                return True
            elapsed_hours = (time.time() - last_run) / 3600.0
            return elapsed_hours >= self.RECALL_INTERVAL_HOURS
        except Exception as e:
            logger.debug("memory_recall_scheduler.should_run_check_failed",
                         error=str(e))
            return False

    async def _tick(self) -> None:
        # DND 时段跳过
        if self._is_dnd():
            return

        # 间隔检查
        if not await self._should_run():
            return

        # MemoryManager 未就绪时跳过（但更新 cron_last_run 避免短时间内反复检查）
        if not self.core.memory:
            logger.debug("memory_recall_scheduler.skip_no_memory_manager")
            try:
                await self.core.db.set_cron_last_run(self.CRON_TASK_NAME)
            except Exception:
                logger.debug("memory_recall_scheduler.cron_last_run_set_failed", exc_info=True)
            return

        # 读取 config 覆盖默认参数（方便运行时调整）
        import config
        hours_back = float(getattr(config, "RECALL_HOURS_BACK", self.HOURS_BACK))
        min_importance = float(getattr(config, "RECALL_MIN_IMPORTANCE", self.MIN_IMPORTANCE))
        min_memories = int(getattr(config, "RECALL_MIN_MEMORIES", self.MIN_MEMORIES))

        # 执行回忆任务
        try:
            processed = await self.core.memory.run_scheduled_recall(
                hours_back=hours_back,
                min_importance=min_importance,
                min_memories=min_memories,
            )
            if processed > 0:
                logger.info("memory_recall_scheduler.completed",
                            processed=processed,
                            hours_back=hours_back)
            # 无论是否触发都更新 cron_last_run：
            # - 触发了：正常记录
            # - 未触发（记忆不足）：避免下个 tick 立刻又检查，浪费 SQL
            await self.core.db.set_cron_last_run(self.CRON_TASK_NAME)
        except Exception as e:
            logger.warning("memory_recall_scheduler.run_failed", error=str(e))
            # 失败也写 cron_last_run，避免短时间内反复重试失败
            try:
                await self.core.db.set_cron_last_run(self.CRON_TASK_NAME)
            except Exception:
                logger.debug("recall_scheduler.cron_last_run_failed", exc_info=True)
