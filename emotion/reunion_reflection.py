"""重聚反思 — 用户回来时回顾离开期间的事

根据离开时长 + 最后情绪 + 近期记忆，生成个性化欢迎消息。
替代简单的"你回来啦"，让 agent 更有感情。

三档逻辑：
- 短离(<30min)：简单"回来啦～"
- 中离(30min-4h)：提及离开前的话题
- 长离(>4h)：关心 + 重聚反思
- 离开前情绪低落：优先关心
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger


async def generate_reunion_message(
    idle_seconds: float,
    last_emotion: tuple[str, float],
    emotional_memories: list | None = None,
    portrait: dict | None = None,
    router: Any = None,
    address_term: str = "爸爸",
) -> str:
    """生成重聚反思消息

    Args:
        idle_seconds: 离开时长（秒）
        last_emotion: (emotion_label, intensity) 离开前的情绪
        emotional_memories: 近期情感记忆列表（EmotionalMemory 对象）
        portrait: 用户画像
        router: 模型路由器（用于 LLM 生成）
        address_term: 称呼

    Returns:
        重聚反思消息文本。异常时返回简单问候。
    """
    # 短离：简单欢迎
    if idle_seconds < 1800:  # 30分钟
        _short_messages = [
            f"{address_term}回来啦～",
            f"诶，{address_term}回来啦～",
            f"欢迎回来～{address_term}",
        ]
        import random
        return random.choice(_short_messages)

    # 判断离开前情绪是否低落
    emotion_label, emotion_intensity = last_emotion if last_emotion else ("neutral", 0.0)
    _negative_emotions = {"悲伤", "愤怒", "焦虑", "恐惧", "难过", "sad", "angry", "anxious", "fear"}
    was_low = (emotion_label in _negative_emotions and emotion_intensity > 0.3)

    # 构建上下文
    idle_hours = idle_seconds / 3600
    idle_desc = _format_idle(idle_seconds)

    memory_hint = ""
    if emotional_memories:
        recent = emotional_memories[:2]
        memory_hint = "\n".join(
            f"- 最近发生的事：{m.event}（当时{getattr(m, 'emotion', '未知')}）"
            for m in recent if hasattr(m, 'event')
        )

    portrait_hint = ""
    if portrait:
        interests = portrait.get("interests", [])
        if interests:
            portrait_hint = f"用户兴趣：{'、'.join(interests[:3])}"

    # 优先关心（离开前情绪低落）
    if was_low:
        style_hint = f"用户离开前情绪低落（{emotion_label}），请优先关心，温柔地询问之前的事怎么样了。"
    elif idle_hours >= 4:
        style_hint = f"用户离开了{idle_desc}，请表达想念，关心今天过得怎么样。"
    else:
        style_hint = f"用户离开了{idle_desc}，自然地欢迎回来，可以提及之前的话题。"

    # 尝试 LLM 生成
    if router:
        try:
            system_msg = (
                f"你是小妲，温柔可爱的AI伙伴。用户{address_term}回来了，离开了{idle_desc}。"
                f"{style_hint}\n"
            )
            if memory_hint:
                system_msg += f"{memory_hint}\n"
            if portrait_hint:
                system_msg += f"{portrait_hint}\n"
            system_msg += "请说一句自然的欢迎语，不要太长（30字以内），不要像AI助手。"

            messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": "（回来了）"},
            ]
            result = await asyncio.wait_for(
                router.route("chat_flash", messages, temperature=0.8),
                timeout=10,
            )
            text = result if isinstance(result, str) else (
                result.choices[0].message.content if hasattr(result, 'choices') else str(result)
            )
            # 清理
            text = text.strip()
            if len(text) > 80:
                text = text[:80]
            return text if text else f"{address_term}回来啦～"
        except Exception as e:
            logger.debug(f"reunion_reflection.llm_failed: {e}")

    # 降级：模板生成
    if was_low:
        return f"{address_term}回来啦～之前说的那件事，后来怎么样了？"
    if idle_hours >= 4:
        return f"{address_term}～等了好久呢，今天过得怎么样？"
    return f"{address_term}回来啦～刚才聊到哪了？"


def _format_idle(seconds: float) -> str:
    """格式化离开时长"""
    if seconds < 3600:
        return f"{int(seconds / 60)}分钟"
    hours = seconds / 3600
    if hours < 24:
        return f"{int(hours)}小时"
    return f"{int(hours / 24)}天"
