"""自发回忆模块 —— 让 agent 在空闲时随机想起过去的事。

与 MemoryRecallScheduler 的区别：
- RecallScheduler：每 3 小时整理回忆笔记（批量整理，结构化）
- SpontaneousRecall：每小时随机想 1 条记忆（单条回忆，内心独白）

机制：
1. 每小时从 episodic_memories 随机抽 1 条（优先 importance > 0.5 的）
2. 用 LLM 生成"内心独白"（第一人称回忆）
3. 回忆保持只读，不因随机想起而强化记忆
4. 不把随机回忆写入自我模型的成长轨迹

这让 agent 有了"内心生活"，即使不聊天时也在"回忆"。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

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


class SpontaneousRecall:
    """自发回忆调度器。"""

    TICK_SECONDS = 3600  # 每小时检查一次
    DND_START_HOUR = 0   # 凌晨免打扰
    DND_END_HOUR = 7

    def __init__(self, core: AgentCore) -> None:
        self.core = core
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("spontaneous_recall.started", interval_hours=1)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _loop(self) -> None:
        """主循环：每小时触发一次自发回忆。"""
        # 启动后等 10 分钟再开始（避免启动高峰）
        await asyncio.sleep(600)
        while self._running:
            try:
                if not self._is_dnd():
                    await self._recall_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("spontaneous_recall.error", error=str(e))
            await asyncio.sleep(self.TICK_SECONDS)

    def _is_dnd(self) -> bool:
        """凌晨免打扰。"""
        hour = _get_local_now().hour
        if self.DND_START_HOUR <= self.DND_END_HOUR:
            return self.DND_START_HOUR <= hour < self.DND_END_HOUR
        return hour >= self.DND_START_HOUR or hour < self.DND_END_HOUR

    async def _recall_once(self) -> None:
        """执行一次自发回忆。"""
        if not self.core.memory:
            return

        try:
            # 从数据库随机抽 1 条记忆（优先 importance > 0.5）
            memory = await self._fetch_random_memory()
            if not memory:
                return

            # 用 LLM 生成内心独白
            monologue = await self._generate_monologue(memory)
            if not monologue:
                return

            logger.info("spontaneous_recall.done",
                        memory_id=memory.get("id"),
                        monologue_len=len(monologue),
                        reinforced=False)
        except Exception as e:
            logger.debug("spontaneous_recall.failed", error=str(e))

    async def _fetch_random_memory(self) -> dict | None:
        """从数据库随机抽 1 条记忆（优先 importance > 0.5）。"""
        try:
            db = self.core.db
            if not db:
                return None
            # 优先从 importance > 0.5 的记忆中随机抽
            row = await db.fetch_one(
                "SELECT * FROM episodic_memories WHERE importance > 0.5 "
                "ORDER BY RANDOM() LIMIT 1"
            )
            if not row:
                # 降级：从所有记忆中随机抽
                row = await db.fetch_one(
                    "SELECT * FROM episodic_memories ORDER BY RANDOM() LIMIT 1"
                )
            return dict(row) if row else None
        except Exception as e:
            logger.debug("spontaneous_recall.fetch_failed", error=str(e))
            return None

    async def _generate_monologue(self, memory: dict) -> str:
        """用 LLM 生成内心独白（第一人称回忆）。"""
        if not self.core.router:
            return ""

        summary = memory.get("summary", "")
        if not summary:
            return ""

        # 时间信息
        ts = memory.get("timestamp", 0)
        if ts:
            try:
                import datetime
                dt = datetime.datetime.fromtimestamp(float(ts))
                time_str = dt.strftime("%Y年%m月%d日")
            except (ValueError, TypeError, OSError):
                time_str = "之前"
        else:
            time_str = "之前"

        prompt = f"""你是小妲，正在独自回忆过去。以下是一条旧记忆的摘要：

时间：{time_str}
内容：{summary[:200]}

请用第一人称写一段简短的内心独白（1-2句话），回忆这件事。语气要自然、有感情，像真的在想过去的事。不要加任何前缀，直接写独白本身。"""

        try:
            result = await asyncio.wait_for(
                self.core.router.route(
                    "chat_flash",
                    [
                        {"role": "system", "content": "你是小妲，在独自回忆过去。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    max_tokens=100,
                ),
                timeout=10.0,
            )
            if isinstance(result, str):
                return result.strip()
        except TimeoutError:
            logger.debug("spontaneous_recall.monologue_timeout")
        except Exception as e:
            logger.debug("spontaneous_recall.monologue_failed", error=str(e))
        return ""
