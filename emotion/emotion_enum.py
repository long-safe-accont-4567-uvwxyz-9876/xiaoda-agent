"""情感系统统一 — 单一枚举源

9 种核心情绪 + TTS 风格层映射 + 中文/变体别名表

当 EMOTION_UNIFIED=off 时，本模块不参与流程；
当 EMOTION_UNIFIED=on（默认），所有情绪相关模块统一使用本枚举。
"""
import os
import re
from enum import Enum


class Emotion(str, Enum):
    """核心情绪枚举（9 种，与 sticker 8 类 + neutral 对齐）"""
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    ANXIOUS = "anxious"
    SHY = "shy"
    CURIOUS = "curious"
    THINKING = "thinking"
    FEAR = "fear"
    NEUTRAL = "neutral"


# 中文/变体 → 核心枚举映射表
EMOTION_ALIASES: dict[str, Emotion] = {
    # happy
    "喜悦": Emotion.HAPPY, "开心": Emotion.HAPPY, "快乐": Emotion.HAPPY,
    "高兴": Emotion.HAPPY, "愉快": Emotion.HAPPY, "欣喜": Emotion.HAPPY,
    "感激": Emotion.HAPPY, "期待": Emotion.HAPPY, "兴奋": Emotion.HAPPY,
    "excited": Emotion.HAPPY, "joy": Emotion.HAPPY, "glad": Emotion.HAPPY,
    "greeting": Emotion.HAPPY,
    # sad
    "悲伤": Emotion.SAD, "难过": Emotion.SAD, "伤心": Emotion.SAD,
    "忧郁": Emotion.SAD, "孤独": Emotion.SAD, "抑郁": Emotion.SAD,
    "可惜": Emotion.SAD, "遗憾": Emotion.SAD, "失落": Emotion.SAD,
    "lonely": Emotion.SAD, "depressed": Emotion.SAD,
    # angry
    "愤怒": Emotion.ANGRY, "生气": Emotion.ANGRY, "恼怒": Emotion.ANGRY,
    "不满": Emotion.ANGRY, "烦躁": Emotion.ANGRY,
    # anxious
    "焦虑": Emotion.ANXIOUS, "紧张": Emotion.ANXIOUS, "不安": Emotion.ANXIOUS,
    "担心": Emotion.ANXIOUS, "忧虑": Emotion.ANXIOUS,
    "worried": Emotion.ANXIOUS, "nervous": Emotion.ANXIOUS,
    # shy
    "害羞": Emotion.SHY, "羞涩": Emotion.SHY, "腼腆": Emotion.SHY,
    "embarrassed": Emotion.SHY,
    # curious
    "好奇": Emotion.CURIOUS, "疑惑": Emotion.CURIOUS, "想知道": Emotion.CURIOUS,
    "interested": Emotion.CURIOUS,
    # thinking
    "思考": Emotion.THINKING, "沉思": Emotion.THINKING, "琢磨": Emotion.THINKING,
    "pondering": Emotion.THINKING,
    # fear
    "恐惧": Emotion.FEAR, "害怕": Emotion.FEAR, "惊恐": Emotion.FEAR,
    "scared": Emotion.FEAR, "afraid": Emotion.FEAR,
    "surprised": Emotion.FEAR,  # 惊讶归入 fear（TTS 专属的 surprised 降级到 fear）
    # neutral
    "平静": Emotion.NEUTRAL, "淡然": Emotion.NEUTRAL, "冷静": Emotion.NEUTRAL,
    "calm": Emotion.NEUTRAL, "ok": Emotion.NEUTRAL, "fine": Emotion.NEUTRAL,
    # TTS 专属值降级映射
    "caring": Emotion.HAPPY,   # 关心 → happy
    "playful": Emotion.HAPPY,  # 俏皮 → happy
}

# TTS 风格层映射：核心枚举 → TTS 细分风格
TTS_STYLE_MAP: dict[Emotion, str] = {
    Emotion.HAPPY: "happy",
    Emotion.SAD: "sad",
    Emotion.ANGRY: "angry",
    Emotion.ANXIOUS: "anxious",
    Emotion.SHY: "shy",
    Emotion.CURIOUS: "curious",
    Emotion.THINKING: "thinking",
    Emotion.FEAR: "fear",
    Emotion.NEUTRAL: "neutral",
}

# sticker 降级映射：核心枚举 → sticker 类别（部分枚举无对应表情包）
STICKER_FALLBACK: dict[Emotion, str] = {
    Emotion.HAPPY: "happy",
    Emotion.SAD: "sad",
    Emotion.ANGRY: "angry",
    Emotion.ANXIOUS: "sad",      # 焦虑降级到 sad 表情
    Emotion.SHY: "shy",
    Emotion.CURIOUS: "curious",
    Emotion.THINKING: "thinking",
    Emotion.FEAR: "fear",
    Emotion.NEUTRAL: "happy",    # neutral 用 happy 表情（最安全的默认）
}

# 合法标签值集合（用于 _ensure_emotion_tag 校验）
VALID_EMOTION_TAGS: set[str] = {e.value for e in Emotion}

# 中文标签 → 英文枚举值的映射（用于 agent_core 中的 emotion_label 转换）
CN_TO_EN: dict[str, str] = {
    "喜悦": "happy",
    "悲伤": "sad",
    "愤怒": "angry",
    "焦虑": "anxious",
    "害羞": "shy",
    "好奇": "curious",
    "思考": "thinking",
    "恐惧": "fear",
    "平静": "neutral",
}


def is_unified() -> bool:
    """检查是否启用统一情感系统"""
    return os.getenv("EMOTION_UNIFIED", "on").lower() in ("on", "1", "true", "yes")


def resolve_emotion(label: str) -> Emotion:
    """将任意情绪标签/中文词解析为核心枚举

    优先匹配枚举值，再查别名表，最后 fallback 到 NEUTRAL
    """
    label = label.strip().lower()
    # 直接匹配枚举值
    try:
        return Emotion(label)
    except ValueError:
        pass
    # 查别名表
    if label in EMOTION_ALIASES:
        return EMOTION_ALIASES[label]
    return Emotion.NEUTRAL


def ensure_emotion_tag(text: str) -> tuple[str, Emotion]:
    """确保文本包含合法的 [emotion:xxx] 标签

    Returns:
        (tagged_text, emotion) — 带标签的文本和解析后的情绪
    """
    # 提取已有标签
    match = re.search(r'\[emotion:([^\]]+)\]', text)
    if match:
        raw_label = match.group(1)
        emotion = resolve_emotion(raw_label)
        if raw_label != emotion.value:
            # 修正为标准标签
            text = text.replace(f"[emotion:{raw_label}]", f"[emotion:{emotion.value}]")
        return text, emotion

    # 无标签：用 emotion_simple 推断
    try:
        from .emotion_simple import detect_emotion
        cn_label = detect_emotion(text)
        if isinstance(cn_label, dict):
            cn_label = cn_label.get("primary", "平静")
        emotion = resolve_emotion(cn_label)
    except Exception:
        emotion = Emotion.NEUTRAL

    # 在文本末尾追加标签
    tagged = text.rstrip() + f" [emotion:{emotion.value}]"
    return tagged, emotion
