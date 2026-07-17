"""情感系统统一 — 单一枚举源

9 种核心情绪 + TTS 风格层映射 + 中文/变体别名表

当 EMOTION_UNIFIED=off 时，本模块不参与流程；
当 EMOTION_UNIFIED=on（默认），所有情绪相关模块统一使用本枚举。
"""
import os
import re
from enum import Enum
from loguru import logger


class Emotion(str, Enum):
    """核心情绪枚举（16 种，覆盖更细腻的表情包分类）"""
    HAPPY = "happy"
    EXCITED = "excited"
    LOVE = "love"
    SHY = "shy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    CONFUSED = "confused"
    THINKING = "thinking"
    PLAYFUL = "playful"
    MOVED = "moved"
    NEUTRAL = "neutral"
    ANXIOUS = "anxious"
    FEAR = "fear"
    CURIOUS = "curious"  # 兼容旧值，降级到 confused
    POUT = "pout"  # 撒娇/娇嗔


# 中文/变体 → 核心枚举映射表
EMOTION_ALIASES: dict[str, Emotion] = {
    # happy（温和的开心）
    "喜悦": Emotion.HAPPY, "开心": Emotion.HAPPY, "快乐": Emotion.HAPPY,
    "高兴": Emotion.HAPPY, "愉快": Emotion.HAPPY, "欣喜": Emotion.HAPPY,
    "感激": Emotion.HAPPY, "期待": Emotion.HAPPY,
    "joy": Emotion.HAPPY, "glad": Emotion.HAPPY, "greeting": Emotion.HAPPY,
    # excited（兴奋/高能量开心）
    "兴奋": Emotion.EXCITED, "激动": Emotion.EXCITED, "惊喜": Emotion.EXCITED,
    "大笑": Emotion.EXCITED, "欢呼": Emotion.EXCITED,
    "excited": Emotion.EXCITED, "thrilled": Emotion.EXCITED,
    # love（喜爱/心动）
    "喜欢": Emotion.LOVE, "爱": Emotion.LOVE, "心动": Emotion.LOVE,
    "喜爱": Emotion.LOVE, "爱慕": Emotion.LOVE, "示爱": Emotion.LOVE,
    "love": Emotion.LOVE, "adore": Emotion.LOVE,
    # sad（悲伤/难过）
    "悲伤": Emotion.SAD, "难过": Emotion.SAD, "伤心": Emotion.SAD,
    "忧郁": Emotion.SAD, "孤独": Emotion.SAD, "抑郁": Emotion.SAD,
    "可惜": Emotion.SAD, "遗憾": Emotion.SAD, "失落": Emotion.SAD,
    "委屈": Emotion.SAD, "晕眩": Emotion.SAD, "难受": Emotion.SAD,
    "lonely": Emotion.SAD, "depressed": Emotion.SAD,
    # angry（愤怒）
    "愤怒": Emotion.ANGRY, "生气": Emotion.ANGRY, "恼怒": Emotion.ANGRY,
    "不满": Emotion.ANGRY, "烦躁": Emotion.ANGRY,
    # anxious（焦虑）
    "焦虑": Emotion.ANXIOUS, "紧张": Emotion.ANXIOUS, "不安": Emotion.ANXIOUS,
    "担心": Emotion.ANXIOUS, "忧虑": Emotion.ANXIOUS,
    "worried": Emotion.ANXIOUS, "nervous": Emotion.ANXIOUS,
    # shy（害羞）
    "害羞": Emotion.SHY, "羞涩": Emotion.SHY, "腼腆": Emotion.SHY,
    "embarrassed": Emotion.SHY,
    # surprised（惊讶/惊恐）
    "惊讶": Emotion.SURPRISED, "吃惊": Emotion.SURPRISED, "震惊": Emotion.SURPRISED,
    "惊恐": Emotion.FEAR, "害怕": Emotion.FEAR, "恐惧": Emotion.FEAR,
    "surprised": Emotion.SURPRISED, "scared": Emotion.SURPRISED, "afraid": Emotion.SURPRISED,
    # confused（困惑/疑惑）
    "困惑": Emotion.CONFUSED, "疑惑": Emotion.CONFUSED, "不解": Emotion.CONFUSED,
    "迷茫": Emotion.CONFUSED, "无语": Emotion.CONFUSED,
    "想知道": Emotion.CONFUSED,
    "confused": Emotion.CONFUSED,
    # curious（好奇）— 与confused区分，好奇是积极认知状态
    "好奇": Emotion.CURIOUS, "curious": Emotion.CURIOUS, "interested": Emotion.CURIOUS,
    # thinking（思考/腹黑）
    "思考": Emotion.THINKING, "沉思": Emotion.THINKING, "琢磨": Emotion.THINKING,
    "阴沉": Emotion.THINKING, "腹黑": Emotion.THINKING,
    "pondering": Emotion.THINKING,
    # playful（调皮/搞怪/得意/傲娇）
    "调皮": Emotion.PLAYFUL, "搞怪": Emotion.PLAYFUL, "俏皮": Emotion.PLAYFUL,
    "得意": Emotion.PLAYFUL, "傲娇": Emotion.PLAYFUL, "卖萌": Emotion.PLAYFUL,
    "playful": Emotion.PLAYFUL, "mischievous": Emotion.PLAYFUL, "proud": Emotion.PLAYFUL,
    # moved（感动/欣慰）
    "感动": Emotion.MOVED, "欣慰": Emotion.MOVED, "暖心": Emotion.MOVED,
    "破涕为笑": Emotion.MOVED,
    "moved": Emotion.MOVED, "touched": Emotion.MOVED,
    # pout（撒娇/娇嗔）
    "撒娇": Emotion.POUT, "娇嗔": Emotion.POUT, "嘟嘴": Emotion.POUT,
    "撅嘴": Emotion.POUT, "耍赖": Emotion.POUT,
    "pout": Emotion.POUT, "coquettish": Emotion.POUT,
    # neutral（中性/平静/无聊）
    "平静": Emotion.NEUTRAL, "淡然": Emotion.NEUTRAL, "冷静": Emotion.NEUTRAL,
    "无聊": Emotion.NEUTRAL, "困倦": Emotion.NEUTRAL, "发呆": Emotion.NEUTRAL,
    "无奈": Emotion.NEUTRAL,
    "calm": Emotion.NEUTRAL, "ok": Emotion.NEUTRAL, "fine": Emotion.NEUTRAL, "bored": Emotion.NEUTRAL,
    # TTS 专属值降级映射
    "caring": Emotion.HAPPY,
    # fear → FEAR
    "fear": Emotion.FEAR,
}

# TTS 风格层映射：核心枚举 → TTS 细分风格
TTS_STYLE_MAP: dict[Emotion, str] = {
    Emotion.HAPPY: "happy",
    Emotion.EXCITED: "happy",     # 兴奋降级到 happy（TTS 无 excited 风格）
    Emotion.LOVE: "happy",        # 喜爱降级到 happy
    Emotion.SAD: "sad",
    Emotion.ANGRY: "angry",
    Emotion.ANXIOUS: "anxious",
    Emotion.SHY: "shy",
    Emotion.SURPRISED: "fear",    # 惊讶用 fear 风格
    Emotion.CONFUSED: "thinking",  # 困惑用 thinking 风格（BUG-2 修复: 原为curious语义不匹配）
    Emotion.THINKING: "thinking",
    Emotion.PLAYFUL: "playful",   # 调皮用独立风格
    Emotion.MOVED: "caring",      # 感动用 caring（温柔关切）
    Emotion.NEUTRAL: "neutral",
    Emotion.FEAR: "fear",
    Emotion.CURIOUS: "curious",
    Emotion.POUT: "coquettish",   # 撒娇用 coquettish 风格
}

# sticker 降级映射：核心枚举 → sticker 类别（部分枚举无对应表情包）
STICKER_FALLBACK: dict[Emotion, str] = {
    Emotion.HAPPY: "happy",
    Emotion.EXCITED: "excited",
    Emotion.LOVE: "love",
    Emotion.SHY: "shy",
    Emotion.SAD: "sad",
    Emotion.ANGRY: "angry",
    Emotion.ANXIOUS: "anxious",
    Emotion.SURPRISED: "surprised",
    Emotion.CONFUSED: "confused",
    Emotion.THINKING: "thinking",
    Emotion.PLAYFUL: "playful",
    Emotion.MOVED: "moved",
    Emotion.NEUTRAL: "neutral",
    Emotion.FEAR: "fear",          # 恐惧 → fear
    Emotion.CURIOUS: "confused",   # 旧 curious → 新 confused
    Emotion.POUT: "pout",          # 撒娇 → pout
}

# 合法标签值集合（用于 _ensure_emotion_tag 校验）
VALID_EMOTION_TAGS: set[str] = {e.value for e in Emotion}

# 中文标签 → 英文枚举值的映射（用于 agent_core 中的 emotion_label 转换）
CN_TO_EN: dict[str, str] = {
    "喜悦": "happy",
    "兴奋": "excited",
    "喜欢": "love",
    "害羞": "shy",
    "悲伤": "sad",
    "愤怒": "angry",
    "惊讶": "surprised",
    "困惑": "confused",
    "思考": "thinking",
    "调皮": "playful",
    "感动": "moved",
    "撒娇": "pout",
    "焦虑": "anxious",
    "平静": "neutral",
    "好奇": "curious",
    "恐惧": "fear",
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
    logger.debug("emotion_enum.resolve: label={!r} not in enum or aliases, fallback to NEUTRAL", label)
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
