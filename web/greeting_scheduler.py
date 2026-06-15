"""GreetingScheduler — Web 通道的定时/随机问候与免打扰（R10）。

独立于 QQ 通道的 NudgeEngine（其闲置问候逻辑保持不变）。
每 30s tick 一次：
- fixed 计划：当天属于 days 且当前时间命中 time（±tick 窗口）且今日未发 → 触发
- random 计划：每天首 tick 时在窗口内抽签 count_per_day 个时刻（避开 DND），命中即触发
- DND：config_service 的 schedule.dnd_periods，多段、支持跨午夜；
  被拦截的 fixed/random 问候在 DND 结束后 10 分钟内补发一次
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime

from loguru import logger


# 推理模型（DeepSeek-R1/MiMo Pro 等）会输出 <think>...</think> 思维链；
# 部分模型只在前缀输出未闭合的"嗯，用户..."等思考文本。统一清洗。
_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_PREFIX_PATTERNS = [
    re.compile(r"^\s*<think\b[^>]*>.*", re.DOTALL | re.IGNORECASE),  # 未闭合 <think>
    re.compile(r"^\s*(嗯[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),  # CoT 段落（"嗯，用户..."）
    re.compile(r"^\s*(首先[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),  # CoT 段落（"首先..."）
]


def _strip_thinking(text: str) -> str:
    """移除推理模型的思维链输出，仅保留最终回复。"""
    if not text:
        return ""
    # 1. 完整 <think>...</think> 标签
    text = _THINK_TAG_RE.sub("", text)
    # 2. 未闭合的 <think> 或 CoT 前缀段落
    for pat in _THINK_PREFIX_PATTERNS:
        m = pat.match(text)
        if m:
            text = text[m.end():]
            break
    return text.strip()


def _hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


class GreetingScheduler:
    TICK_SECONDS = 30

    def __init__(self, core, config_service, broadcast):
        self.core = core
        self.cfg = config_service
        self.broadcast = broadcast  # async callable(event: dict)
        self._task: asyncio.Task | None = None
        self._fired_today: dict[str, set[int]] = {}  # date -> schedule ids fired
        self._deferred: list[dict] = []  # DND 拦截待补发

    def start(self):
        if not self._task:
            self._task = asyncio.create_task(self._loop())
            logger.info("greeting_scheduler.started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.warning("greeting_scheduler.tick_error error={}", str(e))
            await asyncio.sleep(self.TICK_SECONDS)

    # ── 核心逻辑 ─────────────────────────────────────────

    def is_dnd(self, now_min: int | None = None) -> bool:
        if now_min is None:
            now = datetime.now()
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
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = await self.core.db.fetch_one(
            "SELECT COUNT(*) AS c FROM greeting_log WHERE fired_at >= ?", (midnight,))
        return int(row["c"]) if row else 0

    async def _tick(self):
        if not self.cfg.get("schedule.enabled", True):
            return
        now = datetime.now()
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
        if self._deferred and not self.is_dnd(now_min):
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
                    fire_times = json.loads(row["next_fire_times"] or "[]")
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

    async def _generate(self, hint: str) -> str:
        now = datetime.now()
        period = ("清晨" if now.hour < 9 else "上午" if now.hour < 12 else
                  "中午" if now.hour < 14 else "下午" if now.hour < 18 else
                  "傍晚" if now.hour < 20 else "夜晚")
        prompt = (
            f"现在是{period} {now.strftime('%H:%M')}。请以纳西妲的口吻主动向爸爸发一句简短温柔的问候"
            f"（30字以内，不要列表不要解释）。"
        )
        if hint:
            prompt += f"问候主题提示：{hint}。"
        try:
            result = await self.core.router.route(
                "chat_flash",
                [{"role": "system", "content": "你是纳西妲，温柔聪慧，称呼用户为爸爸。直接输出最终回复，不要思考过程。"},
                 {"role": "user", "content": prompt}],
                max_tokens=200)
            text = result if isinstance(result, str) else \
                (result.choices[0].message.content or "")
            text = _strip_thinking(text).strip()
            if text:
                return text[:100]
        except Exception as e:
            logger.warning("greeting.generate_failed error={}", str(e))
        return f"爸爸，{period}好呀～纳西妲在这里陪着你哦 🌱"

    async def _send_qq(self, text: str) -> str | None:
        """发 QQ 主动消息。成功返回 None，失败返回错误描述（供测试接口回显）。"""
        try:
            from qq_bot_adapter import send_proactive_message
            await send_proactive_message(text)
            return None
        except Exception as e:
            logger.warning("greeting.qq_send_failed error={}", str(e))
            return str(e)[:120]
