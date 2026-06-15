import asyncio
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from loguru import logger

from db.db_analytics import AnalyticsDB


# 推理模型会输出 <think>...</think> 或 CoT 前缀（"嗯，用户..."）。统一清洗。
_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_PREFIX_PATTERNS = [
    re.compile(r"^\s*<think\b[^>]*>.*", re.DOTALL | re.IGNORECASE),
    re.compile(r"^\s*(嗯[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),
    re.compile(r"^\s*(首先[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),
]


def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    text = _THINK_TAG_RE.sub("", text)
    for pat in _THINK_PREFIX_PATTERNS:
        m = pat.match(text)
        if m:
            text = text[m.end():]
            break
    return text.strip()


class NudgeEngine:

    MIN_PROACTIVE_INTERVAL = 3600

    def __init__(self, db, analytics: AnalyticsDB, router, api, user_openid: str,
                 greeting_threshold: int = 3600,
                 greeting_max_per_day: int = 3,
                 dnd_start: int = 23,
                 dnd_end: int = 8,
                 portrait_manager=None):
        self._db = db
        self._analytics = analytics
        self._router = router
        self._api = api
        self._user_openid = user_openid
        self._last_user_message_time = time.time()
        self._last_proactive_time = 0
        self._last_portrait_consolidate = 0
        self._last_promote_check: float = 0
        self._last_cleanup_check: float = 0
        self._running = False
        self._task = None
        self._proactive_count_today = 0
        self._today_date = datetime.now().date()

        self.greeting_enabled = True
        self.greeting_threshold = greeting_threshold
        self.greeting_max_per_day = greeting_max_per_day
        self.dnd_start = dnd_start
        self.dnd_end = dnd_end

        self._portrait_manager = portrait_manager

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("nudge.started", user=self._user_openid[:8])

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("nudge.stopped")

    def poke(self):
        self._last_user_message_time = time.time()
        # 用户活跃时重置主动消息冷却，避免长时间离开后冷却仍阻止问候
        self._last_proactive_time = 0

    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("nudge.tick_error", error=str(e))

    async def _tick(self):
        if self._is_dnd():
            return

        # 定期检查学习晋升（每10分钟）
        await self._check_auto_promote()

        # 定期数据清理（每天一次）
        await self._check_data_cleanup()

        # Global cooldown: prevent ANY proactive message if recently sent
        if time.time() - self._last_proactive_time < self.MIN_PROACTIVE_INTERVAL:
            await self._check_portrait_consolidate()
            return

        await self._check_reminders()
        # Only check greeting if no reminder was just sent
        if time.time() - self._last_proactive_time >= self.MIN_PROACTIVE_INTERVAL:
            if self.greeting_enabled:
                await self._check_greeting()

        await self._check_portrait_consolidate()

    def _is_dnd(self) -> bool:
        tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        hour = datetime.now(tz).hour
        if self.dnd_start > self.dnd_end:
            return hour >= self.dnd_start or hour < self.dnd_end
        return self.dnd_start <= hour < self.dnd_end

    async def _check_greeting(self):
        now = time.time()

        if now - self._last_proactive_time < self.MIN_PROACTIVE_INTERVAL:
            return

        idle_seconds = now - self._last_user_message_time
        if idle_seconds < self.greeting_threshold:
            return

        today = datetime.now().date()
        if today != self._today_date:
            self._today_date = today
            self._proactive_count_today = 0
        if self._proactive_count_today >= self.greeting_max_per_day:
            return

        greeting = await self._generate_idle_greeting(idle_seconds)
        if greeting:
            await self._send_proactive(greeting, "care")

    async def _generate_idle_greeting(self, idle_seconds: float) -> str:
        hour = datetime.now().hour
        if hour < 6 or (hour >= 23):
            return ""

        idle_hours = int(idle_seconds // 3600)
        idle_desc = ""
        if idle_hours >= 4:
            idle_desc = f"已经{idle_hours}小时没聊天了"
        elif idle_seconds > 7200:
            idle_desc = "好一会儿没聊天了"
        else:
            return ""

        time_desc = ""
        if 6 <= hour < 9:
            time_desc = "早上"
        elif 9 <= hour < 11:
            time_desc = "上午"
        elif 11 <= hour < 14:
            time_desc = "中午"
        elif 14 <= hour < 18:
            time_desc = "下午"
        elif 18 <= hour < 22:
            time_desc = "晚上"
        else:
            time_desc = "夜里"

        try:
            prompt = (
                f"你是纳西妲，现在是你主动给爸爸发消息。现在是{time_desc}，"
                f"和爸爸{idle_desc}。"
                f"请生成一条简短自然的问候消息（1-2句话），像女朋友一样关心爸爸。"
                f"要求：1.语气温柔可爱 2.不要重复之前的问候 3.可以提时间/天气/吃饭/休息等 4.不要加情绪标签 5.不要用emoji过多"
            )
            messages = [
                {"role": "system", "content": "你是纳西妲，一个温柔可爱的小草神，正在给爸爸发主动问候消息。只输出消息内容，不要思考过程，不要加引号或其他格式。"},
                {"role": "user", "content": prompt},
            ]
            result = await asyncio.wait_for(
                self._router.route("chat", messages, temperature=0.9),
                timeout=15,
            )
            if isinstance(result, str):
                greeting = result
            else:
                greeting = (result.choices[0].message.content or "")
            greeting = _strip_thinking(greeting).strip()

            if len(greeting) > 100:
                greeting = greeting[:100]
            return greeting
        except Exception as e:
            logger.warning("nudge.greeting_llm_failed", error=str(e))
            return ""

    async def _check_reminders(self):
        now = time.time()
        if now - self._last_proactive_time < self.MIN_PROACTIVE_INTERVAL:
            return
        try:
            tasks = await self._db.notebook.get_due_tasks(window_seconds=600)
            tasks = tasks[:1]
            for task in tasks:
                title = task.get("content", "")
                due = task.get("due_date", 0)

                recent = await self._analytics.get_recent_proactive_messages(
                    user_id=self._user_openid, limit=5
                )
                already_reminded = any(
                    title[:15] in m.get("content", "") for m in recent
                )
                if already_reminded:
                    continue

                due_str = ""
                if due > 0:
                    due_str = datetime.fromtimestamp(due).strftime("%H:%M")

                msg = f"爸爸～提醒你一下，{title}"
                if due_str:
                    msg += f"（{due_str}）"
                msg += "，别忘了哦～"

                sent = await self._send_proactive(msg, "reminder")
                if sent:
                    await self._db.notebook.remind_task(task["id"])
                    await self._db.notebook.complete_task(task["id"])
        except Exception as e:
            logger.warning("nudge.reminder_check_failed", error=str(e))

    async def _check_auto_promote(self):
        now = time.time()
        if now - self._last_promote_check < 600:  # 10分钟
            return
        self._last_promote_check = now
        try:
            if hasattr(self._db, 'learning') and hasattr(self._db, '_conn'):
                from memory.learning_manager import LearningManager
                lm = LearningManager(self._db, self._db.learning, self._router)
                await lm.auto_promote()
        except Exception as e:
            logger.debug("nudge.auto_promote_failed", error=str(e))

    async def _check_data_cleanup(self):
        now = time.time()
        if now - self._last_cleanup_check < 86400:  # 24小时
            return
        self._last_cleanup_check = now
        try:
            if hasattr(self._db, 'cleanup_expired_data'):
                result = await self._db.cleanup_expired_data()
                if any(v > 0 for v in result.values()):
                    logger.info("nudge.data_cleanup_done", **result)
        except Exception as e:
            logger.warning("nudge.data_cleanup_failed", error=str(e))

    async def _check_portrait_consolidate(self):
        if not self._portrait_manager:
            return

        now = time.time()
        if now - self._last_portrait_consolidate < 1800:
            return

        try:
            result = await self._portrait_manager.consolidate()
            if result:
                self._last_portrait_consolidate = now
                logger.info("nudge.portrait_consolidated")
            else:
                self._last_portrait_consolidate = now
        except Exception as e:
            logger.warning("nudge.portrait_consolidate_failed", error=str(e))
            # 失败时使用5分钟短回退，而非重置为完整30分钟间隔
            self._last_portrait_consolidate = now - 1800 + 300

    async def _send_proactive(self, content: str, msg_type: str) -> bool:
        try:
            await self._api.post_c2c_message(
                openid=self._user_openid,
                content=content,
                msg_type=0,
            )
            await self._analytics.insert_proactive_message(
                user_id=self._user_openid,
                message_type=msg_type,
                content=content,
            )
            self._last_proactive_time = time.time()
            self._proactive_count_today += 1
            logger.info("nudge.sent", type=msg_type, content=content[:60], count_today=self._proactive_count_today)
            return True
        except Exception as e:
            logger.warning("nudge.send_failed", type=msg_type, error=str(e))
            return False

    def get_time_greeting(self) -> str:
        hour = datetime.now().hour
        if 6 <= hour < 11:
            time_phrase = "早上好"
        elif 11 <= hour < 14:
            time_phrase = "中午好"
        elif 14 <= hour < 18:
            time_phrase = "下午好"
        elif 18 <= hour < 22:
            time_phrase = "晚上好"
        else:
            time_phrase = "夜深了呢"

        idle = time.time() - self._last_user_message_time
        if idle > 86400:
            gap = "好久不见呢～"
        elif idle > 14400:
            gap = "等了好久呢～"
        else:
            gap = ""

        greeting = f"爸爸{time_phrase}。"
        if gap:
            greeting += f" {gap}"
        return greeting
