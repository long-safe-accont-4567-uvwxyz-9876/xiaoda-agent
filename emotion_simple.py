from loguru import logger


_POSITIVE_KEYWORDS = {
    "开心", "高兴", "好耶", "哈哈", "太棒了", "喜欢", "爱", "谢谢",
    "幸福", "快乐", "真好", "不错", "厉害", "好开心", "嘿嘿", "嘻",
}

_NEGATIVE_KEYWORDS = {
    "难过", "累", "烦", "焦虑", "害怕", "孤独", "不开心", "哭",
    "伤心", "崩溃", "绝望", "痛苦", "抑郁", "受不了", "想哭", "好烦",
    "无聊", "郁闷", "沮丧", "失落", "压力", "烦躁",
}

_ANXIOUS_KEYWORDS = {
    "焦虑", "担心", "害怕", "紧张", "不安", "恐惧", "慌",
}


def detect_emotion(text: str) -> dict:
    if not text or not text.strip():
        return {"primary": "平静", "valence": "neutral", "intensity": 0.0}

    pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
    anx_hits = sum(1 for kw in _ANXIOUS_KEYWORDS if kw in text)

    if pos_hits > neg_hits and pos_hits > anx_hits:
        primary = "喜悦"
        valence = "positive"
        intensity = min(0.3 + pos_hits * 0.15, 1.0)
    elif anx_hits > neg_hits:
        primary = "焦虑"
        valence = "negative"
        intensity = min(0.3 + anx_hits * 0.2, 1.0)
    elif neg_hits > 0:
        primary = "悲伤"
        valence = "negative"
        intensity = min(0.3 + neg_hits * 0.15, 1.0)
    else:
        primary = "平静"
        valence = "neutral"
        intensity = 0.0

    return {
        "primary": primary,
        "valence": valence,
        "intensity": intensity,
        "pos_hits": pos_hits,
        "neg_hits": neg_hits,
    }


def build_emotion_hint(emotion: dict) -> str:
    valence = emotion.get("valence", "neutral")
    intensity = emotion.get("intensity", 0.0)

    if valence == "positive" and intensity > 0.5:
        return "伙伴现在心情很好，可以轻快地回应"
    elif valence == "positive":
        return "伙伴心情不错"
    elif valence == "negative" and intensity > 0.5:
        return "伙伴现在情绪比较低落，要温柔地陪伴，不要说教"
    elif valence == "negative":
        return "伙伴有些低落，轻轻陪着就好"
    else:
        return ""
