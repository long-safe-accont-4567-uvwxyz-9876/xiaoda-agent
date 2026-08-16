"""GreetingMixin —— Phase 1 拆分自 message_processor.py。

包含问候短路（_try_greeting_shortcut / _build_greeting_result）、
reunion_reflection 重聚欢迎（_try_reunion_greeting）及问候相关模块级工具函数
（_time_greeting_for_hour / _is_greeting_enabled / _force_close_incomplete_reply）。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared / agent_core.mixins.voice
及 config/core 叶子模块，不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from agent_core._shared import DEGRADED_REPLY, ProcessResult
from agent_core.mixins.voice import _decide_tts_trigger
from config import TTS_ASYNC_MODE
from core.degradation_strategy import get_degradation_strategy
from utils.text_utils import ends_with_valid_ending

# ── G1: 问候短路（模块级编译正则，一次编译多次使用） ───────────
_GREETING_PATTERN = re.compile(
    r'^(你好|您好|hi|hello|hey|嗨|在吗|在不在|在么|'
    r'早安|早上好|早|午安|下午好|晚上好|晚安|'
    r'谢谢|感谢|thanks|thx|多谢)\s*[!！。.～~？?]*$',
    re.IGNORECASE
)

_THANK_REPLIES = ["不客气～", "不用谢啦～", "举手之劳～"]
_THANK_KEYWORDS = ("谢谢", "感谢", "thanks", "thx", "多谢")

# reunion_reflection: 用户"回来了"类关键词（用于生成个性化重聚欢迎）
_REUNION_PATTERN = re.compile(
    r'(回来了|我回来了|回来啦|我回来啦|回来了吗|到家了|上线了)',
    re.IGNORECASE,
)

# G1: 项目硬约束 —— 所有时间函数使用 Asia/Shanghai 时区
_SH_TZ = ZoneInfo("Asia/Shanghai")

# 时段问候：(start_hour, end_hour, reply) — end_hour 可超过 24 以覆盖跨夜时段
_TIME_GREETINGS = [
    (5, 12, "早上好～新的一天开始啦，今天也要加油哦！"),
    (12, 18, "下午好～今天过得怎么样呀？"),
    (18, 22, "晚上好～今天辛苦啦，有什么想聊的吗？"),
    (22, 30, "夜深啦～记得早点休息哦，有什么事明天再说？"),
]


def _time_greeting_for_hour(now_hour: int) -> str:
    """按 Asia/Shanghai 时段返回问候语；未命中回退到最后一项（覆盖 0-5 点）。"""
    for start_h, end_h, msg in _TIME_GREETINGS:
        if start_h <= now_hour < end_h:
            return msg
    return _TIME_GREETINGS[-1][2]


def _is_greeting_enabled() -> bool:
    """读取 ENABLE_GREETING_SHORTCUT 开关（默认 false）。

    用户反馈：模板回复（"早上好～新的一天开始啦"）缺乏上下文感知，
    不如让 LLM 生成自然、有人格温度的问候。默认关闭，让"你好"走 LLM。
    如需恢复模板短路，设置 ENABLE_GREETING_SHORTCUT=true。
    """
    return os.environ.get("ENABLE_GREETING_SHORTCUT", "false").lower() in ("true", "1", "yes")


def _force_close_incomplete_reply(reply: str) -> tuple[str, str]:
    """最终兜底：空回复降级，非空但不以合法标记结尾时追加句号。

    返回 (final_reply, action)，action ∈ {"degraded", "force_closed", "unchanged"}，
    供调用方按 action 记录对应的观测日志。
    """
    if not reply.strip():
        return DEGRADED_REPLY, "degraded"
    final = reply.rstrip()
    if not ends_with_valid_ending(final):
        return final + "。", "force_closed"
    return final, "unchanged"

class GreetingMixin:
    """问候相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    def _try_greeting_shortcut(self, user_input: str, user_id: str, source: str) -> ProcessResult | None:
        """G1: 纯问候短路 - 跳过 LLM 直接返回 <100ms.

        - 默认开启（ENABLE_GREETING_SHORTCUT=true）
        - 群聊跳过（避免刷屏）
        - 问候不超过 20 字符才命中（避免"你好帮我写代码"误命中）
        - 感谢类返回随机感谢回复，其他返回时段问候

        Returns:
            ProcessResult | None: 命中返回 ProcessResult(emotion="greeting")，否则 None
        """
        if not _is_greeting_enabled():
            return None
        # 群聊跳过（避免刷屏）
        if source and "group" in source.lower():
            return None
        text = (user_input or "").strip()
        if not text or len(text) > 20:  # 问候不超过 20 字符
            return None
        match = _GREETING_PATTERN.match(text)
        if not match:
            return None
        keyword = match.group(1).lower()
        # G1: 使用 Asia/Shanghai 时区，避免受系统时区影响
        reply = _time_greeting_for_hour(datetime.now(_SH_TZ).hour)
        # 感谢类覆盖时段问候
        if keyword in _THANK_KEYWORDS:
            reply = random.choice(_THANK_REPLIES)
        return self._build_greeting_result(reply)

    def _build_greeting_result(self, reply: str) -> ProcessResult:
        """构造问候短路结果，语音模式下按 TTS 触发时机决定是否设 tts_pending。

        P1-6 + CodeRabbit F4: 语音模式下问候也要走 TTS 路径，但必须与
        _build_voice_result 的条件对齐：voice_mode + tts.available +
        TTS_ASYNC_MODE + is_feature_available("tts")。
        TTS 时机控制 v2：统一过 _decide_tts_trigger（移除冷却，改为内容适宜性守卫）。
        """
        if (TTS_ASYNC_MODE
                and _decide_tts_trigger(
                    reply, force_voice=False, voice_mode=self._voice_mode,
                    tts_available=self.tts.available,
                    tts_enabled=get_degradation_strategy().is_feature_available("tts"))):
            return ProcessResult(
                reply=reply, emotion="greeting",
                tts_pending=True, tts_text=reply,
            )
        return ProcessResult(reply=reply, emotion="greeting")

    async def _try_reunion_greeting(self, user_input: str, user_id: str,
                                    user_openid: str) -> ProcessResult | None:
        """reunion_reflection 接线：用户"回来了"检测，生成个性化重聚欢迎。

        - 仅命中"回来了/我回来了"等关键词时触发
        - idle 时长从最近 session 的 ended_at 计算，last_emotion 从 mental_state 读取
        - 任何异常回退 None，不阻塞主流程（后续仍走正常 LLM 路径）
        """
        text = (user_input or "").strip()
        if not text or len(text) > 20:
            return None
        if not _REUNION_PATTERN.search(text):
            return None
        try:
            # 1. idle_seconds：从最近 session 的 ended_at 计算
            idle_seconds = 0.0
            _uid = user_openid or user_id
            if _uid and getattr(self, "db", None) is not None:
                try:
                    cursor = await self.db._conn.execute(
                        "SELECT ended_at FROM sessions WHERE user_openid=? "
                        "ORDER BY ended_at DESC LIMIT 1",
                        (_uid,),
                    )
                    row = await cursor.fetchone()
                    if row and row[0]:
                        idle_seconds = max(0.0, time.time() - float(row[0]))
                except Exception:
                    idle_seconds = 0.0
            # 2. last_emotion：从 mental_state 的 user_last_emotion 读取
            last_emotion = ("neutral", 0.0)
            try:
                from core.mental_state import get_mental_state_manager_if_exists
                mgr = get_mental_state_manager_if_exists(user_id=user_id)
                if mgr is not None:
                    _label = getattr(mgr.state.S, "user_last_emotion", "")
                    if _label:
                        last_emotion = (_label, 0.5)
            except Exception as e:
                logger.debug("reunion_reflection.last_emotion_failed", error=str(e))
            # 3. 生成重聚欢迎消息（内部有降级模板，router 缺失也能返回）
            from emotion.reunion_reflection import generate_reunion_message
            reply = await generate_reunion_message(
                idle_seconds=idle_seconds,
                last_emotion=last_emotion,
                router=getattr(self, "router", None),
                address_term=getattr(self.context, "current_address_term", "爸爸"),
            )
            return ProcessResult(reply=reply, emotion="greeting")
        except Exception:
            logger.debug("reunion_reflection.failed")
            return None

