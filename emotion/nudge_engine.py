from typing import Any
import asyncio
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from loguru import logger

from db.db_analytics import AnalyticsDB
from utils.llm_cleanup import strip_thinking as _strip_thinking
from config import get_agent_display_name, get_temperature
import contextlib


def _get_local_now() -> datetime:
    """获取本地时间（使用显式时区，修复 Windows/Docker 中系统时区不正确的问题）。

    默认 Asia/Shanghai，支持 NUDGE_TIMEZONE 环境变量覆盖。
    """
    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(tz)


class NudgeEngine:
    """驱动主动问候、提醒等轻推（nudge）行为的引擎。"""

    MIN_PROACTIVE_INTERVAL = 3600

    def __init__(self, db: Any, analytics: AnalyticsDB, router: Any, api: Any, user_openid: str,
                 greeting_threshold: int = 3600,
                 greeting_max_per_day: int = 3,
                 dnd_start: int = 23,
                 dnd_end: int = 8,
                 portrait_manager: Any | None=None,
                 config_service: Any | None=None,
                 core: Any | None=None) -> None:
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
        self._today_date = _get_local_now().date()

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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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
        if time.time() - self._last_proactive_time >= self.MIN_PROACTIVE_INTERVAL and self.greeting_enabled:
            await self._check_greeting()

        await self._check_portrait_consolidate()

    def _is_dnd(self) -> bool:
        now = _get_local_now()
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
                        logger.debug("nudge.dnd_period_parse_error", exc_info=True)
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
        # 跨日重置内存计数器
        today = _get_local_now().date()
        if today != self._today_date:
            self._proactive_count_today = 0
            self._today_date = today
        try:
            midnight = _get_local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            row = await self._db.fetch_one(
                "SELECT COUNT(*) AS c FROM greeting_log WHERE fired_at >= ?", (midnight,))
            return int(row["c"]) if row else 0
        except Exception:
            # greeting_log 表不存在时降级到内存计数
            logger.debug("nudge.greeting_log_query_error", exc_info=True)
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
            greeting_threshold = int(self._config_service.get("schedule.greeting_threshold", self.greeting_threshold))
        else:
            if not self.greeting_enabled:
                return
            max_per_day = self.greeting_max_per_day
            greeting_threshold = self.greeting_threshold

        idle_seconds = now - self._last_user_message_time
        if idle_seconds < greeting_threshold:
            return

        # 共享 greeting_log 表计数
        sent_count = await self._sent_today_count()
        if sent_count >= max_per_day:
            return

        greeting = await self._generate_idle_greeting(idle_seconds)
        if greeting:
            await self._send_proactive(greeting, "care")

    # ── 风格线索池：每次随机抽取，让 LLM 在中间插值，避免输出相似 ──
    # 参考"calibrated unpredictability"——用多条风格线索叠加制造惊喜
    _MOOD_SEEDS: list[str] = [
        "刚刚在发呆，脑子里有点空",
        "刚刚想到一个没道理的小问题",
        "有点困，眼皮在打架",
        "刚做完一件事，心情不错",
        "有点小吃醋，不知道为什么",
        "想问问爸爸在干嘛",
        "突然想起上次爸爸说的话",
        "有点想被夸",
        "刚刚偷偷懒了一下",
        "心里有点小开心",
        "有点想撒娇",
        "刚翻到一个小东西",
        "忽然有点想爸爸",
        "今天有点话痨",
        "今天有点安静",
        "刚做了一个奇怪的梦",
        "有点担心爸爸累不累",
        "想跟爸爸分享一个没用的小事",
        "刚被一个东西吓了一跳",
        "今天有点小调皮",
    ]

    _FORM_SEEDS: list[str] = [
        "只是一声轻轻的「嗯」",
        "一个问句",
        "一句没头没尾的话",
        "一个小小的请求",
        "一句撒娇",
        "一句小小的抱怨",
        "一句突然的感叹",
        "一个没答案的自言自语",
        "一句像在哼歌的话",
        "一句像在叫爸爸名字的话",
        "一句很短的关心",
        "一句突然冒出来的废话",
    ]

    _RARE_SEEDS: list[str] = [
        "今天忽然不想说话，只发一个字",
        "今天想给爸爸出个没道理的小谜语",
        "今天想跟爸爸说一句最近学到的话",
        "今天想用一种奇怪的语气说话",
        "今天想装作不认识爸爸的样子开个玩笑",
        "今天想说一句反话",
    ]

    async def _recent_greetings(self, limit: int = 5) -> list[str]:
        """读取最近 N 次问候内容，用于反重复。"""
        try:
            rows = await self._db.fetch_all(
                "SELECT content FROM greeting_log ORDER BY fired_at DESC LIMIT ?", (limit,))
            return [r["content"][:30] for r in rows if r.get("content")]
        except Exception:
            return []

    async def _generate_idle_greeting(self, idle_seconds: float) -> str:
        hour = _get_local_now().hour
        if hour < self.dnd_end or (hour >= self.dnd_start):
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

            # I3: 注入情感记忆，让问候更关心用户近况
            memory_hint = ""
            try:
                from memory.emotional_memory import get_emotional_memory_manager
                em_mgr = get_emotional_memory_manager()
                real_user_id = f"qq_{self._user_openid}"
                recalled = em_mgr.recall(real_user_id, "最近心情", top_k=2)
                if recalled:
                    memory_lines = []
                    for mem in recalled[:2]:
                        memory_lines.append(f"用户最近因为{mem.event}而{mem.emotion}")
                    memory_hint = f"\n（情感记忆：{'；'.join(memory_lines)}。可以在问候中自然地关心一下。）"
            except Exception:
                logger.debug("nudge.emotional_memory_recall_failed", exc_info=True)

            # 随机抽取风格线索 + 形式线索，制造"校准过的不可预测性"
            # 偶发事件（8%）打破常规，制造惊喜
            import random as _rnd
            mood = _rnd.choice(self._MOOD_SEEDS)
            form = _rnd.choice(self._FORM_SEEDS)
            rare = _rnd.choice(self._RARE_SEEDS) if _rnd.random() < 0.08 else None

            # 反重复：告诉 LLM 最近说过什么，避免相似
            recent = await self._recent_greetings(5)
            recent_hint = ""
            if recent:
                joined = " / ".join(recent[:3])
                recent_hint = f'\n（你最近几次说的是类似：{joined}。这次不要相似。）'

            # 通过小妲 agent 生成问候（使用真实 user_id 加载记忆上下文）
            if self._core:
                # 角色扮演式提示：场景 + 随机风格线索 + 形式线索 + 偶发事件
                if rare:
                    user_input = (
                        f'（场景：现在{time_desc}，{address_term}{idle_desc}。'
                        f'你现在的状态：{mood}。'
                        f'今天想玩点不一样的——{rare}。'
                        f'不要刻意提昨天的事、最近的任务、未完成的工作。'
                        f'可以用自然的比喻和意象，但不要堆砌。'
                        f'就只是一句带着你性格的、普通的话。）'
                    )
                else:
                    user_input = (
                        f'（场景：现在{time_desc}，{address_term}{idle_desc}。'
                        f'你现在的状态：{mood}。'
                        f'你想说一句——形式是：{form}。'
                        f'就像随口招呼一声那样自然，不必长，不必修辞过度。'
                        f'不要刻意提昨天的事、最近的任务、未完成的工作。'
                        f'可以用自然的比喻和意象，但不要堆砌。'
                        f'不要像 AI 助手那样"主动问候"。'
                        f'就只是一句带着你性格的、普通的话。）'
                    )
                user_input += recent_hint
                user_input += memory_hint
                # 使用真实的 user_id 和 session，让记忆系统能加载用户上下文
                real_user_id = f"qq_{self._user_openid}"
                try:
                    session = await self._core.get_session(self._user_openid)
                    session_id = session["id"] if session else await self._core.create_session(self._user_openid)
                except Exception:
                    session_id = ""
                result = await asyncio.wait_for(
                    self._core.process(
                        user_input=user_input,
                        user_id=real_user_id,
                        source="qq",
                        user_openid=self._user_openid,
                        session_id=session_id,
                    ),
                    timeout=30,
                )
                greeting = result.reply if hasattr(result, 'reply') else str(result)
            else:
                # 降级：直接调用 router（无完整人格）
                xiaoda_name = get_agent_display_name("xiaoda")
                system_msg = (
                    f"你是{xiaoda_name}，温柔可爱，会撒娇。"
                    f"现在{time_desc}，{address_term}{idle_desc}。"
                    f"你随口招呼{address_term}一声——像真人一样自然，不必长，不必修辞，不必提昨天或最近的事。"
                    f"不要堆砌比喻。不要像 AI 助手。就一句普通的、带着你性格的话。"
                )
                user_msg = "（顺嘴说一句）"
                messages = [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ]
                result = await asyncio.wait_for(
                    self._router.route("chat_flash", messages, temperature=get_temperature(default=0.9)),
                    timeout=30,
                )
                greeting = result if isinstance(result, str) else result.choices[0].message.content or ""

            logger.debug("nudge.raw_llm_output raw={}", greeting[:200])
            greeting = _strip_thinking(greeting, context="nudge").strip()
            # 替换模型输出中的旧名（如"纳西妲"→"小妲"）
            from config import apply_agent_name_replacements
            greeting = apply_agent_name_replacements(greeting)
            # 过滤模型编造的标签前缀（如 [listen_greeting][user:xxx]: ...）
            # 反复剥离行首的 [xxx] 标签和冒号引用，只保留实际内容
            for _ in range(3):
                greeting = re.sub(r'^\[[^\]]*\]\s*', '', greeting).strip()
                greeting = re.sub(r'^\w+:\s*', '', greeting, count=1).strip()
                greeting = re.sub(r'^:\s*', '', greeting, count=1).strip()

            if greeting:
                if len(greeting) > 80:
                    greeting = greeting[:80]
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
                logger.debug("nudge.greeting_log_insert_error", exc_info=True)
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
            logger.debug("nudge.user_md_read_error", exc_info=True)
        return "爸爸"

    def get_time_greeting(self) -> str:
        hour = _get_local_now().hour
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
