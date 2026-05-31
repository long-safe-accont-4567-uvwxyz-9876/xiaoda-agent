import re
from loguru import logger


class EmotionAnalyzer:

    EMOTION_KEYWORDS = {
        "happy": ["开心", "高兴", "快乐", "幸福", "太好了", "哈哈", "嘻嘻", "棒", "赞"],
        "sad": ["难过", "伤心", "悲伤", "哭", "不开心", "失望", "遗憾"],
        "angry": ["生气", "愤怒", "烦", "讨厌", "气死"],
        "surprise": ["惊讶", "天啊", "哇", "不会吧", "真的吗", "厉害"],
        "grateful": ["谢谢", "感谢", "感恩", "多谢"],
        "confused": ["困惑", "不懂", "什么意思", "为什么", "怎么"],
    }

    def analyze(self, text: str) -> dict:
        text_lower = text.lower()
        scores = {}
        for emotion, keywords in self.EMOTION_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > 0:
                scores[emotion] = score

        if not scores:
            return {"emotion": "neutral", "confidence": 0.5}

        dominant = max(scores, key=scores.get)
        total = sum(scores.values())
        confidence = scores[dominant] / total if total > 0 else 0.5
        return {"emotion": dominant, "confidence": round(confidence, 2)}

    def get_response_style(self, emotion: str) -> str:
        styles = {
            "happy": "分享旅行者的喜悦，用温暖的语气回应",
            "sad": "温柔地安慰旅行者，给予支持",
            "angry": "理解旅行者的感受，冷静地分析问题",
            "surprise": "一起惊叹，分享好奇心",
            "grateful": "谦虚地接受感谢，表达关心",
            "confused": "耐心地解释，用简单的语言",
            "neutral": "正常交流",
        }
        return styles.get(emotion, "正常交流")
