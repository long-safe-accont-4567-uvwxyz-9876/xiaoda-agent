from typing import Any, Optional
import asyncio
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from loguru import logger

from db.db_analytics import AnalyticsDB
from utils.llm_cleanup import strip_thinking as _strip_thinking


class NudgeEngine:

    MIN_PROACTIVE_INTERVAL = 3600

    def __init__(self, db: Any, analytics: AnalyticsDB, router: Any, api: Any, user_openid: str,
                 greeting_threshold: int = 3600,
                 greeting_max_per_day: int = 3,
                 dnd_start: int = 23,
                 dnd_end: int = 8,
                 portrait_manager: Optional[Any]=None,
                 config_service: Optional[Any]=None,
                 core: Optional[Any]=None) -> None:
        self._db = db
        self._analytics = analytics
        self._router = router
        self._api = api
        self._core = core
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
        self._config_service = config_service

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("nudge.started", user=self._user_openid[:8])

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("nudge.stopped")

    def poke(self) -> None:
        self._last_user_message_time = time.time()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("nudge.tick_error", error=str(e))

    async def _tick(self) -> None:
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
        now = datetime.now(tz)
        now_min = now.hour * 60 + now.minute

        # 优先读取 WebUI 配置（与 GreetingScheduler 共享）
        if self._config_service:
            dnd_periods = self._config_service.get("schedule.dnd_periods", [])
            if dnd_periods:
                for p in dnd_periods:
                    try:
                        s_h, s_m = p["start"].split(":")
                        e_h, e_m = p["end"].split(":")
                        s, e = int(s_h) * 60 + int(s_m), int(e_h) * 60 + int(e_m)
                    except Exception:
                        continue
                    if s <= e:
                        if s <= now_min < e:
                            return True
                    else:  # 跨午夜
                        if now_min >= s or now_min < e:
                            return True
                return False

        # 降级：使用环境变量配置
        if self.dnd_start > self.dnd_end:
            return now.hour >= self.dnd_start or now.hour < self.dnd_end
        return self.dnd_start <= now.hour < self.dnd_end

    async def _sent_today_count(self) -> int:
        """查询 greeting_log 表今日已发数量（与 GreetingScheduler 共享计数）。"""
        try:
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            row = await self._db.fetch_one(
                "SELECT COUNT(*) AS c FROM greeting_log WHERE fired_at >= ?", (midnight,))
            return int(row["c"]) if row else 0
        except Exception:
            # greeting_log 表不存在时降级到内存计数
            return self._proactive_count_today

    async def _check_greeting(self) -> None:
        now = time.time()

        if now - self._last_proactive_time < self.MIN_PROACTIVE_INTERVAL:
            return

        # 读取 WebUI 配置
        if self._config_service:
            if not self._config_service.get("schedule.enabled", True):
                return
            max_per_day = int(self._config_service.get("schedule.greeting_max_per_day", self.greeting_max_per_day))
        else:
            if not self.greeting_enabled:
                return
            max_per_day = self.greeting_max_per_day

        idle_seconds = now - self._last_user_message_time
        if idle_seconds < self.greeting_threshold:
            return

        # 共享 greeting_log 表计数
        sent_count = await self._sent_today_count()
        if sent_count >= max_per_day:
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
            address_term = self._get_address_term()

            # 通过纳西妲 agent 生成问候（保持人格一致性）
            if self._core:
                user_input = (
                    f"[主动问候] 现在是{time_desc}，{address_term}{idle_desc}。"
                    f"请主动向{address_term}发一句简短温柔的问候（1-2句话，像女朋友一样关心）。只输出问候语。"
                )
                result = await asyncio.wait_for(
                    self._core.process(
                        user_input=user_input,
                        user_id="nudge_engine",
                        source="qq",
                        user_openid=self._user_openid,
                        session_id="nudge",
                    ),
                    timeout=30,
                )
                greeting = result.reply if hasattr(result, 'reply') else str(result)
            else:
                # 降级：直接调用 router（无完整人格）
                system_msg = (
                    f"你是纳西妲，一个温柔可爱的小草神，正在给{address_term}发主动问候消息。"
                    f"现在是{time_desc}，{address_term}{idle_desc}。"
                    f"直接输出一句简短温柔的问候（1-2句话，30字以内），不要输出任何其他内容。"
                    f"只输出问候语本身，像女朋友一样关心{address_term}。"
                )
                user_msg = f"请以纳西妲的口吻向{address_term}发一句简短温柔的问候。"
                messages = [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ]
                result = await asyncio.wait_for(
                    self._router.route("chat_flash", messages, temperature=0.9),
                    timeout=15,
                )
                if isinstance(result, str):
                    greeting = result
                else:
                    greeting = (result.choices[0].message.content or "")

            logger.debug("nudge.raw_llm_output raw={}", greeting[:200])
            greeting = _strip_thinking(greeting, context="nudge").strip()

            if greeting:
                if len(greeting) > 100:
                    greeting = greeting[:100]
                return greeting
        except Exception as e:
            logger.warning("nudge.greeting_llm_failed", error=str(e))
        return ""

    async def _check_reminders(self) -> None:
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

                msg = f"{self._get_address_term()}～提醒你一下，{title}"
                if due_str:
                    msg += f"（{due_str}）"
                msg += "，别忘了哦～"

                sent = await self._send_proactive(msg, "reminder")
                if sent:
                    await self._db.notebook.remind_task(task["id"])
                    await self._db.notebook.complete_task(task["id"])
        except Exception as e:
            logger.warning("nudge.reminder_check_failed", error=str(e))

    async def _check_auto_promote(self) -> None:
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

    async def _check_data_cleanup(self) -> None:
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

    async def _check_portrait_consolidate(self) -> None:
        if not self._portrait_manager:
            return

        now = time.time()
        if now - self._last_portrait_consolidate < 1800:
            return

        try:
            result = await self._portrait_manager.consolidate(
                address_term=self._get_address_term())
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
            # 同步写入 greeting_log 表，与 GreetingScheduler 共享计数
            try:
                await self._db.execute(
                    "INSERT INTO greeting_log(schedule_id, fired_at, content, channel, reason) "
                    "VALUES (?,?,?,?,?)",
                    (0, time.time(), content, "qq", f"nudge_{msg_type}"))
            except Exception:
                pass  # greeting_log 表不存在时静默忽略
            self._last_proactive_time = time.time()
            self._proactive_count_today += 1
            logger.info("nudge.sent", type=msg_type, content=content[:60], count_today=self._proactive_count_today)
            return True
        except Exception as e:
            logger.warning("nudge.send_failed", type=msg_type, error=str(e))
            return False

    def _get_address_term(self) -> str:
        """读取用户自定义称呼，兜底"爸爸"。

        与 AgentCore._read_address_term_from_user_md 逻辑一致，
        从 USER.md 的"称呼"字段读取，供主动问候/提醒等无上下文场景使用。
        """
        try:
            from config import WORKSPACE_DIR
            user_md = WORKSPACE_DIR / "USER.md"
            if user_md.exists():
                content = user_md.read_text(encoding="utf-8-sig")
                match = re.search(r'-\s*称呼[：:]\s*(.+)', content)
                if match:
                    val = match.group(1).strip()
                    if val and not val.startswith("（") and val not in ("待填写", "主人/朋友/你的名字"):
                        return val
        except Exception:
            pass
        return "爸爸"

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

        greeting = f"{self._get_address_term()}{time_phrase}。"
        if gap:
            greeting += f" {gap}"
        return greeting
