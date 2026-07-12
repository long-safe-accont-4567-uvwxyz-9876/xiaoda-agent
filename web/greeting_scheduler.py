"""GreetingScheduler — Web 通道的定时/随机问候与免打扰（R10）。

独立于 QQ 通道的 NudgeEngine（其闲置问候逻辑保持不变）。
每 30s tick 一次：
- fixed 计划：当天属于 days 且当前时间命中 time（±tick 窗口）且今日未发 → 触发
- random 计划：每天首 tick 时在窗口内抽签 count_per_day 个时刻（避开 DND），命中即触发
- DND：config_service 的 schedule.dnd_periods，多段、支持跨午夜；
  被拦截的 fixed/random 问候在 DND 结束后 10 分钟内补发一次
"""
from __future__ import annotations
from typing import Any

import asyncio
import json
import os
import random
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from utils.llm_cleanup import strip_thinking as _strip_thinking


def _get_local_now() -> datetime:
    """获取本地时间（使用显式时区，修复 Windows/Docker 中系统时区不正确的问题）。

    默认 Asia/Shanghai，支持 NUDGE_TIMEZONE 环境变量覆盖。
    与 emotion/nudge_engine.py 保持一致的时区处理逻辑。
    """
    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.now(tz)


def _hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


class GreetingScheduler:
    TICK_SECONDS = 30

    def __init__(self, core: Any, config_service: Any, broadcast: Any) -> None:
        self.core = core
        self.cfg = config_service
        self.broadcast = broadcast  # async callable(event: dict)
        self._task: asyncio.Task | None = None
        self._fired_today: dict[str, set[int]] = {}  # date -> schedule ids fired
        self._deferred: list[dict] = []  # DND 拦截待补发
        self._deferred_lock = threading.Lock()

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._loop())
            logger.info("greeting_scheduler.started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.warning("greeting_scheduler.tick_error error={}", str(e))
            await asyncio.sleep(self.TICK_SECONDS)

    # ── 核心逻辑 ─────────────────────────────────────────

    def is_dnd(self, now_min: int | None = None) -> bool:
        if now_min is None:
            now = _get_local_now()
            now_min = now.hour * 60 + now.minute
        for p in self.cfg.get("schedule.dnd_periods", []):
            try:
                s, e = _hm_to_min(p["start"]), _hm_to_min(p["end"])
            except Exception:
                continue
            if s <= e:
                if s <= now_min < e:
                    return True
            else:  # 跨午夜
                if now_min >= s or now_min < e:
                    return True
        return False

    async def _sent_today_count(self) -> int:
        midnight = _get_local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = await self.core.db.fetch_one(
            "SELECT COUNT(*) AS c FROM greeting_log WHERE fired_at >= ?", (midnight,))
        return int(row["c"]) if row else 0


    async def _tick(self) -> None:
        if not self.cfg.get("schedule.enabled", True):
            return
        now = _get_local_now()
        today = now.strftime("%Y-%m-%d")
        now_min = now.hour * 60 + now.minute
        weekday = now.isoweekday()  # 1..7
        fired = self._fired_today.setdefault(today, set())
        # 清理旧日期缓存
        for k in list(self._fired_today):
            if k != today:
                del self._fired_today[k]

        rows = await self.core.db.fetch_all(
            "SELECT * FROM greeting_schedules WHERE enabled=1")
        max_per_day = int(self.cfg.get("schedule.greeting_max_per_day", 3))

        # 先处理 DND 补发
        with self._deferred_lock:
            has_deferred = bool(self._deferred)
        if has_deferred and not self.is_dnd(now_min):
            with self._deferred_lock:
                pending, self._deferred = self._deferred, []
            for d in pending:
                if await self._sent_today_count() < max_per_day:
                    await self.fire(d["schedule"], reason=d["reason"] + "_deferred")

        for row in rows:
            sid = row["id"]
            try:
                days = json.loads(row["days"] or "[]")
            except Exception:
                days = list(range(1, 8))
            if weekday not in days:
                continue

            hit = False
            fire_key = sid
            if row["type"] == "fixed" and row["time"]:
                if sid in fired:
                    continue
                target = _hm_to_min(row["time"])
                if target <= now_min < target + max(1, self.TICK_SECONDS // 60 + 1):
                    hit = True
            elif row["type"] == "random":
                # 当日未抽签则抽签
                if row["drawn_date"] != today:
                    times = self._draw_random_times(row)
                    await self.core.db.execute(
                        "UPDATE greeting_schedules SET next_fire_times=?, drawn_date=? WHERE id=?",
                        (json.dumps(times), today, sid))
                    row = dict(row)
                    row["next_fire_times"] = json.dumps(times)
                try:
                    fire_times = json.loads(row.get("next_fire_times", "[]") or "[]")
                except Exception:
                    fire_times = []
                for t in fire_times:
                    key = (sid, t)
                    if t <= now_min < t + 1 and key not in fired:
                        hit = True
                        fire_key = key
                        break

            if not hit:
                continue
            fired.add(fire_key)
            if await self._sent_today_count() >= max_per_day:
                logger.info("greeting.skipped_quota schedule_id={}", sid)
                continue
            if self.is_dnd(now_min):
                with self._deferred_lock:
                    self._deferred.append({"schedule": dict(row), "reason": row["type"]})
                logger.info("greeting.deferred_dnd schedule_id={}", sid)
                continue
            await self.fire(dict(row), reason=row["type"])

    def _draw_random_times(self, row: dict) -> list[int]:
        try:
            ws, we = _hm_to_min(row["window_start"]), _hm_to_min(row["window_end"])
        except Exception:
            return []
        if we <= ws:
            return []
        count = max(1, int(row["count_per_day"] or 1))
        times: list[int] = []
        for _ in range(count):
            for _attempt in range(20):
                t = random.randint(ws, we - 1)
                if not self.is_dnd(t):
                    times.append(t)
                    break
        return sorted(set(times))

    # ── 生成与投递 ───────────────────────────────────────

    async def fire(self, schedule: dict, reason: str = "manual_test") -> str:
        text, _ = await self.fire_with_report(schedule, reason)
        return text

    async def fire_with_report(self, schedule: dict,
                               reason: str = "manual_test") -> tuple[str, dict]:
        """触发问候并返回 (text, 各通道投递结果)。"""
        hint = schedule.get("prompt_hint") or ""
        text = await self._generate(hint)
        try:
            channels = json.loads(schedule.get("channels") or '["web"]')
        except Exception:
            channels = ["web"]
        report: dict[str, dict] = {}
        for ch in channels:
            if ch == "web":
                await self.broadcast({"type": "greeting", "text": text, "reason": reason})
                report["web"] = {"ok": True, "error": None}
            elif ch == "qq":
                err = await self._send_qq(text)
                report["qq"] = {"ok": err is None, "error": err}
        delivered = [c for c, r in report.items() if r["ok"]]
        await self.core.db.execute(
            "INSERT INTO greeting_log(schedule_id, fired_at, content, channel, reason) "
            "VALUES (?,?,?,?,?)",
            (schedule.get("id", 0), time.time(), text, ",".join(delivered) or "none", reason))
        logger.info("greeting.fired reason={} report={} text={}", reason, report, text[:40])
        return text, report

    # ── 风格线索池：每次随机抽取 1-2 条，让 LLM 在中间插值，避免输出相似 ──
    # 设计理念：参考"calibrated unpredictability"——不靠规则堆砌，
    # 而是用多条"风格线索"叠加，模型会在中间自动插值产生惊喜
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

    # 形式线索池：随机选一种形式，打破"问候语"的固定模式
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

    # 偶发事件池（低概率 8%）：偶尔来点意想不到的，制造"眼前一亮"
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
            rows = await self.core.db.fetch_all(
                "SELECT content FROM greeting_log ORDER BY fired_at DESC LIMIT ?", (limit,))
            return [r["content"][:30] for r in rows if r.get("content")]
        except Exception:
            return []

    async def _quality_check(self, text: str) -> bool:
        """用小模型检查问候内容质量（复用 MemoryDistiller 的 GLM-4-9B-0414）。

        检查项：是否自然、是否拼接混乱、是否内容不当。
        返回 True 表示通过，False 表示需要重新生成。
        """
        try:
            import httpx
            api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
            if not api_key:
                return True  # 无 API key 则跳过检查
            messages = [
                {"role": "system", "content": (
                    "你是一个内容质量检查器。判断以下问候消息是否合格。\n"
                    "合格标准：1）语句通顺自然，像真人随口说的话；"
                    "2）没有奇怪的拼接或前后矛盾；"
                    "3）没有不当内容（如涉及食物、物品的暧昧暗示）。\n"
                    "只回复 OK 或 FAIL，不要解释。"
                )},
                {"role": "user", "content": text},
            ]
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    "https://api.siliconflow.cn/v1/chat/completions",
                    json={
                        "model": "THUDM/GLM-4-9B-0414",
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": 10,
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                result = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                passed = "ok" in result.lower()
                if not passed:
                    logger.info("greeting.quality_check_failed text={} result={}", text[:40], result)
                return passed
        except Exception as e:
            logger.debug("greeting.quality_check_error error={}", str(e))
            return True  # 检查出错则放行

    async def _generate(self, hint: str) -> str:
        """通过小妲 agent 生成问候（使用真实 user_id 以加载记忆上下文）。

        流程：生成 → 质量检查（小模型） → 不合格则重试一次 → 仍不合格则用兜底。
        """
        address_term = getattr(self.core.context, "current_address_term", "") or "爸爸"

        # 构建带时间上下文的问候指令
        hour = _get_local_now().hour
        if hour < 6:
            time_hint, activity = "深夜", "还没睡"
        elif hour < 9:
            time_hint, activity = "清晨", "刚醒"
        elif hour < 12:
            time_hint, activity = "上午", "在忙"
        elif hour < 14:
            time_hint, activity = "中午", "要吃饭"
        elif hour < 18:
            time_hint, activity = "下午", "在干活"
        elif hour < 22:
            time_hint, activity = "晚上", "收工了"
        else:
            time_hint, activity = "深夜", "准备睡了"

        for attempt in range(2):  # 最多重试 1 次
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

            # 角色扮演式提示：场景 + 随机风格线索 + 形式线索 + 偶发事件
            if rare:
                scene = (
                    f'（场景：现在{time_hint}，{address_term}大概{activity}了。'
                    f'你现在的状态：{mood}。'
                    f'今天想玩点不一样的——{rare}。'
                    f'不要刻意提昨天的事、最近的任务、未完成的工作。'
                    f'可以用自然的比喻和意象，但不要堆砌。'
                    f'就只是一句带着你性格的、普通的话。）'
                )
            else:
                scene = (
                    f'（场景：现在{time_hint}，{address_term}大概{activity}了。'
                    f'你现在的状态：{mood}。'
                    f'你想说一句——形式是：{form}。'
                    f'就像随口招呼一声那样自然，不必长，不必修辞过度。'
                    f'不要刻意提昨天的事、最近的任务、未完成的工作。'
                    f'可以用自然的比喻和意象，但不要堆砌。'
                    f'不要像 AI 助手那样"主动问候"。'
                    f'就只是一句带着你性格的、普通的话。）'
                )
            user_input = scene + recent_hint
            if hint:
                user_input += f'\n（如果顺嘴能带一句关于「{hint}」的就带，想不到就不带。）'

            try:
                # 使用真实的 user_id 和 session，让记忆系统能加载用户上下文
                user_openid = "webui"
                session = await self.core.get_session(user_openid)
                session_id = session["id"] if session else await self.core.create_session(user_openid)

                result = await self.core.process(
                    user_input=user_input,
                    user_id="webui",
                    source="web",
                    user_openid=user_openid,
                    session_id=session_id,
                )
                text = result.reply if hasattr(result, 'reply') else str(result)
                logger.debug("greeting.raw_output attempt={} hint={} raw={}", attempt, hint, text[:200])
                text = _strip_thinking(text, context="greeting").strip()
                # 替换模型输出中的旧名（如"纳西妲"→"小妲"）
                from config import apply_agent_name_replacements
                text = apply_agent_name_replacements(text)
                # 过滤模型编造的标签前缀（如 [listen_greeting][user:xxx]: ...）
                import re as _re
                for _ in range(3):
                    text = _re.sub(r'^\[[^\]]*\]\s*', '', text).strip()
                    text = _re.sub(r'^\w+:\s*', '', text, count=1).strip()
                    text = _re.sub(r'^:\s*', '', text, count=1).strip()
                if not text:
                    continue

                # 限制长度：真人随口招呼通常很短，过长反而像 AI
                text = text[:80]

                # 内容质量检查（小模型）
                if await self._quality_check(text):
                    return text
                else:
                    logger.info("greeting.quality_retry attempt={}", attempt)
                    continue  # 不合格，重试
            except Exception as e:
                logger.warning("greeting.generate_failed attempt={} error={}", attempt, str(e))
                continue

        # 两次都不合格，用兜底
        return f"{address_term}，好呀～"

    async def _send_qq(self, text: str) -> str | None:
        """发 QQ 主动消息。成功返回 None，失败返回错误描述（供测试接口回显）。"""
        try:
            from qq_bot_adapter import send_proactive_message
            await send_proactive_message(text)
            return None
        except Exception as e:
            logger.warning("greeting.qq_send_failed error={}", str(e))
            return str(e)[:120]