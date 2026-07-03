"""成长叙事模块 —— 每天结束时生成成长总结。

机制：
1. 每天晚上 23:00 触发（TICK 检查）
2. 从 episodic_memories 取当天的记忆
3. 用 LLM 生成成长叙事（学到了什么、有什么变化）
4. 写入 self_model.md 的"成长轨迹"部分
5. 写入长期记忆（作为一条特殊记忆）

这让 agent 有了"成长感"，每天都能回顾自己的变化。
"""
from __future__ import annotations

import asyncio
import time
import datetime
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.core import AgentCore


class GrowthNarrative:
    """每日成长叙事生成器。"""

    TICK_SECONDS = 1800  # 每 30 分钟检查一次
    TRIGGER_HOUR = 23    # 晚上 23:00 触发

    def __init__(self, core: "AgentCore") -> None:
        self.core = core
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_run_date: str = ""  # YYYY-MM-DD，防止重复触发

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("growth_narrative.started", trigger_hour=self.TRIGGER_HOUR)

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
        """主循环：每 30 分钟检查是否到触发时间。"""
        # 启动后等 5 分钟
        await asyncio.sleep(300)
        while self._running:
            try:
                now = datetime.datetime.now()
                today = now.strftime("%Y-%m-%d")
                # 每天 23:00-23:30 之间触发，且当天未执行过
                if (now.hour == self.TRIGGER_HOUR
                        and self._last_run_date != today):
                    await self._generate_daily_narrative()
                    self._last_run_date = today
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("growth_narrative.error", error=str(e))
            await asyncio.sleep(self.TICK_SECONDS)

    async def _generate_daily_narrative(self) -> None:
        """生成当天的成长叙事。"""
        if not self.core.memory or not self.core.router:
            return

        try:
            # 获取今天的记忆
            today_memories = await self._fetch_today_memories()
            if len(today_memories) < 3:
                logger.info("growth_narrative.skip_too_few_memories",
                            count=len(today_memories))
                return

            # 用 LLM 生成成长叙事
            narrative = await self._generate_narrative(today_memories)
            if not narrative:
                return

            # 写入自我模型的成长轨迹
            try:
                from core.self_model import append_growth_entry
                today = datetime.datetime.now().strftime("%Y-%m-%d")
                append_growth_entry(f"今日成长：{narrative[:100]}")
            except Exception as e:
                logger.debug("growth_narrative.self_model_update_failed", error=str(e))

            # 写入长期记忆（作为一条特殊记忆）
            try:
                await self._save_as_memory(narrative)
            except Exception as e:
                logger.debug("growth_narrative.memory_save_failed", error=str(e))

            logger.info("growth_narrative.generated",
                        memory_count=len(today_memories),
                        narrative_len=len(narrative))
        except Exception as e:
            logger.warning("growth_narrative.generate_failed", error=str(e))

    async def _fetch_today_memories(self) -> list[dict]:
        """获取今天的记忆。"""
        try:
            db = self.core.db
            if not db:
                return []
            # 今天的 0 点时间戳
            now = datetime.datetime.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            rows = await db.fetch_all(
                "SELECT * FROM episodic_memories WHERE timestamp >= ? "
                "ORDER BY timestamp DESC LIMIT 30",
                (midnight,)
            )
            return [dict(r) for r in rows] if rows else []
        except Exception as e:
            logger.debug("growth_narrative.fetch_failed", error=str(e))
            return []

    async def _generate_narrative(self, memories: list[dict]) -> str:
        """用 LLM 生成成长叙事。"""
        # 准备记忆摘要
        summaries = []
        for m in memories[:15]:  # 最多 15 条，避免 token 过多
            summary = m.get("summary", "")[:80]
            if summary:
                summaries.append(f"- {summary}")

        if not summaries:
            return ""

        memories_text = "\n".join(summaries)
        today = datetime.datetime.now().strftime("%Y年%m月%d日")

        prompt = f"""今天是{today}。以下是纳西妲今天的记忆摘要：

{memories_text}

请以纳西妲的第一人称视角，写一段简短的成长叙事（50-100字），回答：
1. 今天学到了什么？
2. 有什么想法或感受的变化？

语气要自然、有感情，像在写日记。不要加任何前缀，直接写叙事内容。"""

        try:
            result = await asyncio.wait_for(
                self.core.router.route(
                    "chat_flash",
                    [
                        {"role": "system", "content": "你是纳西妲，在写每日成长日记。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    max_tokens=200,
                ),
                timeout=15.0,
            )
            if isinstance(result, str):
                return result.strip()
        except asyncio.TimeoutError:
            logger.debug("growth_narrative.generate_timeout")
        except Exception as e:
            logger.debug("growth_narrative.generate_failed", error=str(e))
        return ""

    async def _save_as_memory(self, narrative: str) -> None:
        """将成长叙事保存为一条记忆。"""
        try:
            # 构造记忆上下文
            context = {
                "user_input": "（系统：每日成长叙事生成）",
                "assistant_reply": narrative,
                "timestamp": time.time(),
                "session_id": f"growth_{datetime.datetime.now().strftime('%Y%m%d')}",
                "source": "growth_narrative",
            }
            await self.core.memory.encode_memory(context)
        except Exception as e:
            logger.debug("growth_narrative.encode_failed", error=str(e))