from typing import Any
import os
import re
import json
import datetime
import time as _time
import httpx
from loguru import logger

from db.db_notebook import NotebookDB
from config import get_agent_display_name


AUTO_NOTE_PROMPT_TEMPLATE = """你是{agent_name}。刚刚和{address_term}进行了一轮对话。

{address_term}说了：
"{user_message}"

人家回应了：
"{assistant_reply}"

人家已经记下的关于{address_term}的认知：
{existing_notes}

请在下面选择一个行动（只需要返回格式，不需要解释）：

如果这轮对话让你对{address_term}有了新的了解——发现了他的性格特征、生活习惯、偏好倾向、
情感模式或价值观，且已有笔记里没有记过，请返回：INSIGHT: 简短描述
例如：INSIGHT: 性格急躁不喜欢等待
例如：INSIGHT: 经常熬夜作息不规律
例如：INSIGHT: 做事注重效率不爱闲聊
例如：INSIGHT: 压力大时倾向独处

如果{address_term}明确说「提醒我」「帮我记一下」「别忘了」或给了具体时间，
请务必返回：TASK: 任务标题 @ 时间 [@ 重复模式]
例如：TASK: 提醒吃饭 @ 19:00 @ 每天
例如：TASK: 开会 @ 明天14:00
例如：TASK: 周会 @ 周一09:00 @ 每周
例如：TASK: 喝水 @ 9:00 @ 每天

【重要】定时任务会自动注册到系统的定时提醒页面，{address_term}可以在Web UI的
「定时问候」页面查看、编辑或删除。每天的任务会每天按时触发，不会遗漏。

不该记的：
- 日常寒暄（「今天天气不错」「吃了吗」）→ PASS
- 没有揭示{address_term}特征的简单问答 → PASS
- 重复内容（已有笔记里记过的事）→ PASS
- 常识性聊天（「今天吃了个苹果」）→ PASS
- 纯情绪宣泄没有特征信息（「好累啊」）→ PASS

如果只是普通闲聊，或这件事已经在已有笔记里记过了，请返回：PASS"""


class NotebookManager:
    """管理笔记本条目的提取、写入与查询。"""

    def __init__(self, db: Any, notebook: NotebookDB, router: Any) -> None:
        self._db = db
        self.notebook = notebook
        self._router = router
        self._free_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._free_base_url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        self._free_model = "THUDM/GLM-4-9B-0414"  # 非思考模型，避免 Z1 思考碎片污染洞察
        logger.info("notebook.ready")

    async def _call_free_model(self, messages: list, temperature: float = 0.6,
                                max_tokens: int = 800) -> str | None:
        """调用硅基流动免费模型"""
        if not self._free_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self._free_base_url}/chat/completions",
                    json={
                        "model": self._free_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    headers={
                        "Authorization": f"Bearer {self._free_api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("notebook.free_model_failed", error=str(e))
            return None

    async def add_note(self, content: str, tags: list[str] | None = None) -> int:
        due_date = self._extract_due_date(content)
        tags_str = ",".join(tags) if tags else ""
        return await self.notebook.insert_notebook("note", content, tags=tags_str, due_date=due_date)

    def _extract_due_date(self, content: str) -> float:
        patterns = [
            r"下周[一二三四五六日天]",
            r"这周[一二三四五六日天]",
            r"本周[一二三四五六日天]",
            r"明天[早晚上午下午中午]?",
            r"后天[早晚上午下午中午]?",
            r"今天[早晚上午下午中午]?",
            r"\d{1,2}月\d{1,2}[日号]",
        ]
        for pat in patterns:
            m = re.search(pat, content)
            if m:
                ts = self._parse_task_time(m.group())
                if ts and ts > 0:
                    return ts
        return 0

    async def add_focus(self, content: str) -> int:
        await self.notebook.archive_notebook_entries(0, kind="focus")
        return await self.notebook.insert_notebook("focus", content, importance=0.8)

    async def get_current_focus(self) -> str | None:
        try:
            items = await self.notebook.get_notebook_notes(kind="focus", limit=1)
            return items[0]["content"] if items else None
        except Exception:
            logger.debug("notebook.get_current_focus_failed: {}", exc_info=True)
            return None

    async def schedule_task(self, title: str, priority: int = 0, due_at: float = 0.0) -> int:
        return await self.notebook.insert_notebook("task", title, importance=float(priority), due_date=due_at)

    async def get_due_tasks(self, window_seconds: int = 3600) -> list[dict]:
        return await self.notebook.get_due_tasks(window_seconds=window_seconds)

    async def get_pending_tasks(self, limit: int = 20) -> list[dict]:
        return await self.notebook.get_pending_tasks(limit=limit)

    async def get_pending_tasks_summary(self) -> list[str]:
        tasks = await self.notebook.get_pending_tasks(limit=5)
        if not tasks:
            return []
        lines = []
        for t in tasks:
            title = t.get("content", "")[:30]
            due = t.get("due_date", 0)
            if due and due > 0:
                ds = datetime.datetime.fromtimestamp(due).strftime("%H:%M")
                lines.append(f"⏰ {title} @ {ds}")
            else:
                lines.append(f"· {title}")
        return lines

    async def complete_task(self, task_id: int) -> None:
        await self.notebook.complete_task(task_id)

    async def cancel_task(self, task_id: int) -> None:
        await self.notebook.cancel_task(task_id)

    async def delete_note(self, note_id: int) -> bool:
        return await self.notebook.delete_notebook_entry(note_id)

    async def touch_note(self, note_id: int) -> bool:
        return await self.notebook.touch_notebook_entry(note_id)

    async def auto_note_after_message(self, user_msg: str, reply: str,
                                      address_term: str = "爸爸") -> None:
        try:
            existing = await self.get_recent_notes(limit=10)
            if existing:
                lines = [f"· {n['content']}" for n in existing]
                existing_str = "\n".join(lines)
            else:
                existing_str = "（还没有笔记）"

            prompt = AUTO_NOTE_PROMPT_TEMPLATE.format(
                user_message=user_msg[:300],
                assistant_reply=reply[:300],
                existing_notes=existing_str,
                address_term=address_term,
                agent_name=get_agent_display_name("xiaoda"),
            )
            # 优先使用免费模型，降级到主路由
            result = await self._call_free_model(
                [{"role": "user", "content": prompt}],
                temperature=0.6, max_tokens=800,
            )
            if result is None and self._router:
                result = await self._router.route(
                    "memory_encoding",
                    [{"role": "user", "content": prompt}],
                    temperature=0.6,
                    max_tokens=800,
                )
            result = (result or "").strip()

            last_line = result.strip().split("\n")[-1].strip()

            if last_line.startswith("INSIGHT:"):
                content = last_line[8:].strip()
                # 过滤心理学分析/操控类描述（LLM 过度解读用户行为）
                _BAD_INSIGHT_KEYWORDS = (
                    "操控", "诱导", "依赖", "心理", "矛盾", "利用", "暗示",
                    "承认", "正当化", "控制欲", "妥协", "情感", "动机",
                    "合理化", "防御", "投射", "转移",
                )
                if any(kw in content for kw in _BAD_INSIGHT_KEYWORDS):
                    logger.info("notebook.insight_rejected", content=content[:40], reason="psychology_analysis")
                    return
                # 拒绝过长的分析性描述（正常偏好应 <30 字）
                if len(content) > 40:
                    logger.info("notebook.insight_rejected", content=content[:40], reason="too_long")
                    return
                if content and '<' not in content and len(content) > 1:
                    similar_id = await self._find_similar(content)
                    if similar_id:
                        await self.touch_note(similar_id)
                        logger.info("notebook.insight_merged", content=content[:40], note_id=similar_id)
                        return
                    await self.add_note(content)
                    logger.info("notebook.insight", content=content[:40])
            elif last_line.startswith("TASK:"):
                task_str = last_line[5:].strip()
                if task_str and '<' not in task_str:
                    title, time_hm, days, is_one_time = self._parse_task_with_repeat(task_str)
                    if title:
                        await self._create_reminder_schedule(title, time_hm, days, is_one_time)
                        logger.info("notebook.auto_task_as_reminder", title=title[:40],
                                    time=time_hm, days=days, one_time=is_one_time)
            else:
                logger.debug("notebook.auto_note_pass", result=result[:80])
        except Exception as e:
            logger.warning("notebook.auto_note_failed", error=str(e))

    async def _find_similar(self, content: str, threshold: float = 0.5) -> int | None:
        existing = await self.get_recent_notes(limit=20)
        if not existing:
            return None
        new_words = set(content.split())
        for note in existing:
            old_words = set(note.get("content", "").split())
            if not old_words:
                continue
            overlap = len(new_words & old_words) / max(len(new_words | old_words), 1)
            if overlap >= threshold:
                return note["id"]
        return None

    async def get_recent_notes(self, limit: int = 10) -> list[dict]:
        return await self.notebook.get_notebook_notes(limit=limit)

    def _parse_task_time(self, time_str: str) -> float:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import os
        tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        original = time_str.strip()
        time_str = original

        cn_dow = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        for cn, dow in cn_dow.items():
            if f"下周{cn}" in original:
                days_to_next_monday = (7 - now.weekday()) % 7
                if days_to_next_monday == 0:
                    days_to_next_monday = 7
                next_monday = now + datetime.timedelta(days=days_to_next_monday)
                target = next_monday.replace(hour=0, minute=0, second=0, microsecond=0)
                target = target + datetime.timedelta(days=dow)
                return target.timestamp()
            if f"这周{cn}" in original or f"本周{cn}" in original:
                days_since_monday = now.weekday()
                this_monday = now - datetime.timedelta(days=days_since_monday)
                target = this_monday.replace(hour=0, minute=0, second=0, microsecond=0)
                target = target + datetime.timedelta(days=dow)
                if target > now:
                    return target.timestamp()
                return 0.0

        if "后天" in time_str:
            base = now + datetime.timedelta(days=2)
        elif "明天" in time_str or "明早" in time_str or "明晚" in time_str:
            base = now + datetime.timedelta(days=1)
        else:
            base = now

        is_pm = any(w in original for w in ["晚", "夜", "下午"])

        for prefix in ["后天", "明天", "明早", "明晚", "今天", "今早", "今晚", "上午", "下午", "晚上", "中午", "明夜", "今夜"]:
            time_str = time_str.replace(prefix, "").strip()

        cn_num = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
                  "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
                  "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
                  "二十一": 21, "二十二": 22, "二十三": 23}
        for cn, num in cn_num.items():
            if cn in time_str:
                time_str = time_str.replace(cn, str(num))
                break

        try:
            parts = time_str.split(":")
            hour = int(re.sub(r"[^0-9]", "", parts[0]) or "0")
            minute = int(re.sub(r"[^0-9]", "", parts[1])) if len(parts) > 1 else 0
            if is_pm and hour < 12 and hour > 0:
                hour += 12
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                return target.timestamp()
        except (ValueError, IndexError):
            pass

        for cn, num in cn_num.items():
            if time_str.strip() == cn or time_str.strip().startswith(cn):
                hour = num
                if is_pm and hour < 12:
                    hour += 12
                target = base.replace(hour=hour, minute=0, second=0, microsecond=0)
                return target.timestamp()

        return 0.0
    # ── 定时提醒调度（替代 notebook_entries task） ────────────────

    def _parse_task_with_repeat(self, task_str: str) -> tuple[str, str, list[int], bool]:
        """解析 TASK 字符串，提取标题、时间、重复天数、是否一次性。

        返回 (title, time_hm, days, is_one_time):
        - title: 提醒标题
        - time_hm: HH:MM 格式时间
        - days: 触发周几列表 [1..7]（1=周一, 7=周日）
        - is_one_time: 是否一次性（触发后自动禁用）

        示例:
        - "喝水 @ 9:00 @ 每天" → ("喝水", "09:00", [1,2,3,4,5,6,7], False)
        - "开会 @ 明天14:00" → ("开会", "14:00", [明天周几], True)
        - "周会 @ 周一09:00 @ 每周" → ("周会", "09:00", [1], False)
        """
        parts = [p.strip() for p in task_str.split("@") if p.strip()]
        title = parts[0] if parts else ""
        time_str = parts[1] if len(parts) > 1 else ""
        repeat_str = parts[2].lower() if len(parts) > 2 else ""

        # 解析时间
        time_hm = self._extract_hhmm(time_str)
        is_one_time = True
        days = list(range(1, 8))  # 默认每天

        # 解析重复模式
        if repeat_str in ("每天", "每日", "daily", "everyday", "每天重复"):
            is_one_time = False
            days = list(range(1, 8))
        elif repeat_str in ("每周", "weekly", "每周重复"):
            is_one_time = False
            days = self._extract_weekdays(time_str)
        elif "每天" in title or "每日" in title:
            is_one_time = False
            days = list(range(1, 8))
            title = title.replace("每天", "").replace("每日", "").strip()
        else:
            # 检查时间字符串是否包含周几
            week_days = self._extract_weekdays(time_str)
            if week_days and len(week_days) < 7:
                is_one_time = False
                days = week_days
            elif "明天" in time_str or "后天" in time_str:
                # 一次性任务，计算目标周几
                due_ts = self._parse_task_time(time_str)
                if due_ts > 0:
                    from zoneinfo import ZoneInfo
                    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
                    try:
                        tz = ZoneInfo(tz_name)
                    except Exception:
                        tz = ZoneInfo("Asia/Shanghai")
                    target_dt = datetime.datetime.fromtimestamp(due_ts, tz=tz)
                    days = [target_dt.isoweekday()]
                is_one_time = True
            else:
                # 默认视为一次性
                is_one_time = True

        return title, time_hm, days, is_one_time

    def _extract_hhmm(self, time_str: str) -> str:
        """从时间字符串提取 HH:MM 格式。"""
        # 先用 _parse_task_time 获取时间戳
        ts = self._parse_task_time(time_str)
        if ts > 0:
            from zoneinfo import ZoneInfo
            tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("Asia/Shanghai")
            dt = datetime.datetime.fromtimestamp(ts, tz=tz)
            return dt.strftime("%H:%M")
        # 回退：尝试直接匹配 HH:MM
        m = re.search(r'(\d{1,2}):(\d{2})', time_str)
        if m:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
        # 再回退：纯数字小时
        m = re.search(r'(\d{1,2})', time_str)
        if m:
            return f"{int(m.group(1)):02d}:00"
        return "09:00"  # 兜底

    def _extract_weekdays(self, text: str) -> list[int]:
        """从文本提取周几 [1..7]。"""
        cn_dow = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
        en_dow = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
        found = []
        for cn, dow in cn_dow.items():
            if f"周{cn}" in text or f"星期{cn}" in text:
                found.append(dow)
        for en, dow in en_dow.items():
            if en in text.lower():
                found.append(dow)
        return sorted(set(found)) if found else list(range(1, 8))

    async def _create_reminder_schedule(self, title: str, time_hm: str,
                                         days: list[int],
                                         is_one_time: bool = False) -> int | None:
        """在 greeting_schedules 表创建 type='reminder' 的定时提醒。

        替代原来的 schedule_task() → notebook_entries 写入，
        使提醒进入 GreetingScheduler 的 30s tick 精确调度循环。
        """
        if not self._db:
            logger.warning("notebook.create_reminder_no_db")
            return None
        try:
            now = _time.time()
            cursor = await self._db.execute(
                "INSERT INTO greeting_schedules "
                "(type, time, days, prompt_hint, channels, enabled, "
                " next_fire_times, drawn_date, created_at) "
                "VALUES ('reminder', ?, ?, ?, ?, 1, '[]', '', ?)",
                (time_hm, json.dumps(sorted(set(days))), title,
                 json.dumps(["web"]), now),
            )
            await self._db.commit()
            row_id = cursor.lastrowid
            logger.info("notebook.reminder_created", id=row_id, title=title[:40],
                        time=time_hm, days=days, one_time=is_one_time)
            return row_id
        except Exception as e:
            logger.warning("notebook.reminder_create_failed", error=str(e))
            # 降级：回退到 notebook_entries
            due_at = self._parse_task_time(time_hm)
            return await self.schedule_task(title=title, priority=1, due_at=due_at)