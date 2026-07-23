from .pad_model import PADEmotion, from_emotion as pad_from_emotion


# ── 统一后的 9 类关键词集（合并 emotion_simple + sticker_manager 关键词） ──

_POSITIVE_KEYWORDS = {
    # 原有
    "开心", "高兴", "好耶", "哈哈", "太棒了", "谢谢",
    "幸福", "快乐", "真好", "不错", "厉害", "好开心", "嘿嘿", "嘻",
    # 从 sticker_manager happy 合并
    "嘻嘻", "太好了", "耶", "棒", "好高兴", "真棒", "好喜欢",
    "开心～", "嘻嘻～", "嘿嘿～", "好耶！", "太好了！",
    "太开心", "好幸福", "满足", "好满足", "满足～",
    "好快乐", "乐", "超棒", "超好", "超喜欢",
    # love 相关词移至 _LOVE_KEYWORDS，避免喜爱情绪被误判为喜悦
    # "喜欢"/"爱"/"好喜欢"/"超喜欢" 已移至 _LOVE_KEYWORDS
    # 注意: 问候语关键词不再归入喜悦 — 问候是社交礼仪而非情绪表达 (BUG-3 修复)
}

_EXCITED_KEYWORDS = {
    "超开心", "超～开心", "好嗨", "好激动", "兴奋", "期待",
    "太兴奋", "超兴奋", "好兴奋", "激动死了", "嗨翻了",
}

_NEGATIVE_KEYWORDS = {
    # 原有
    "难过", "累", "烦", "孤独", "不开心", "哭",
    "伤心", "崩溃", "绝望", "痛苦", "抑郁", "受不了", "想哭", "好烦",
    "无聊", "郁闷", "沮丧", "失落", "压力", "烦躁",
    # 从 sticker_manager sad 合并
    "呜呜", "555", "呜", "呜～", "5555",
    "呜呜呜", "心碎", "好孤独", "寂寞", "好寂寞", "泪", "哭了",
}

_ANGRY_KEYWORDS = {
    # 原有
    "生气", "愤怒", "气死", "恼火", "火大", "气炸", "暴怒", "气愤",
    "太气了", "气死我", "可恶", "混蛋", "该死", "烦死", "气人",
    # 从 sticker_manager angry 合并
    "哼！", "讨厌", "烦人", "不理你",
    "好气", "讨厌！", "烦！", "气鼓鼓", "哼哼",
    "不要理我", "生气！", "哼", "好烦", "烦死了", "气死我了",
    "好生气",
}

_ANXIOUS_KEYWORDS = {
    # 原有
    "焦虑", "担心", "紧张", "不安", "忧虑",
    # 从 sticker_manager fear 中拆出焦虑类关键词
    "慌", "好紧张", "好担心", "好不安", "心慌", "忐忑", "心神不宁", "提心吊胆",
    "worried", "nervous",
}

_SHY_KEYWORDS = {
    # 从 sticker_manager shy 合并
    "害羞", "脸红", "不好意思", "才没有", "才不是", "///",
    "脸红红", "害羞～", "才不是呢", "不要这样说", "讨厌啦",
    "害羞了", "不好意思～", "才不要", "羞", "好羞",
    "捂脸", "脸热", "脸好热",
}

_CURIOUS_KEYWORDS = {
    # 从 sticker_manager curious 合并
    "好奇", "咦", "嗯？", "什么呀", "为什么", "咦？",
    "真的吗", "是怎样", "怎么回事", "好奇怪", "咦～",
    "为什么呀", "诶", "诶？", "啊？", "什么！？", "不会吧",
    "竟然", "居然",
}

_THINKING_KEYWORDS = {
    # 从 sticker_manager thinking 合并
    "想想", "嗯...", "让我想想", "唔", "这个嘛", "让我看看",
    "唔...", "想一想", "思考", "嗯～", "唔～",
    "让我想想～", "怎么说呢", "让我琢磨", "琢磨",
    "让我思考", "思考一下", "分析一下",
}

_FEAR_KEYWORDS = {
    # 原有（从 _ANXIOUS_KEYWORDS 拆出纯恐惧类）
    "害怕", "恐惧",
    # 从 sticker_manager fear 合并
    "好怕", "好恐惧", "好慌",
    "吓", "吓到", "吓死", "可怕", "好可怕", "慌张",
    "惊恐", "惊吓", "颤抖",
}

# ── 补齐 5 类缺失情绪（BUG 修复: 原仅 9 类，love/playful/moved/pout/surprised 无法检测）──

_LOVE_KEYWORDS = {
    "喜欢", "爱", "心动", "喜爱", "爱慕", "示爱",
    "好喜欢", "超喜欢", "好心动", "好喜欢你",
}

_PLAYFUL_KEYWORDS = {
    "调皮", "搞怪", "俏皮", "得意", "傲娇", "卖萌",
    "嘿嘿嘿", "咯咯", "嘻嘻嘻", "略略",
}

_MOVED_KEYWORDS = {
    "感动", "欣慰", "暖心", "破涕为笑",
    "好感动", "太感动", "暖到了",
}

_POUT_KEYWORDS = {
    "撒娇", "娇嗔", "嘟嘴", "撅嘴", "耍赖",
    "哼～", "才不要嘛", "人家要", "不理你啦",
}

_SURPRISED_KEYWORDS = {
    "惊讶", "吃惊", "震惊", "什么！？",
    "吓一跳", "没想到", "想不到", "万万没想到",
}

# 14 类中文标签（与 Emotion 枚举 + sticker 目录对齐）
_EMOTION_CATEGORIES = [
    # 特定情绪优先于通用喜悦，避免 "喜欢" 被误判为喜悦
    ("喜爱", _LOVE_KEYWORDS),
    ("撒娇", _POUT_KEYWORDS),
    ("调皮", _PLAYFUL_KEYWORDS),
    ("感动", _MOVED_KEYWORDS),
    ("惊讶", _SURPRISED_KEYWORDS),
    ("兴奋", _EXCITED_KEYWORDS),
    ("喜悦", _POSITIVE_KEYWORDS),
    ("悲伤", _NEGATIVE_KEYWORDS),
    ("愤怒", _ANGRY_KEYWORDS),
    ("焦虑", _ANXIOUS_KEYWORDS),
    ("害羞", _SHY_KEYWORDS),
    ("好奇", _CURIOUS_KEYWORDS),
    ("思考", _THINKING_KEYWORDS),
    ("恐惧", _FEAR_KEYWORDS),
]


def detect_emotion(text: str) -> dict:
    """检测文本情绪，返回包含 primary/valence/intensity 的字典

    primary 为 9 种中文标签之一：喜悦/悲伤/愤怒/焦虑/害羞/好奇/思考/恐惧/平静
    """
    if not text or not text.strip():
        return {
            "primary": "平静",
            "valence": "neutral",
            "intensity": 0.0,
            "pad": PADEmotion.neutral().to_dict(),
        }

    # 计算每类命中数
    scores = {}
    for label, keywords in _EMOTION_CATEGORIES:
        scores[label] = sum(1 for kw in keywords if kw in text)

    # 找到命中数最多的类别
    best_label = max(scores, key=scores.get)
    best_hits = scores[best_label]

    if best_hits == 0:
        return {
            "primary": "平静",
            "valence": "neutral",
            "intensity": 0.0,
            "pad": PADEmotion.neutral().to_dict(),
        }

    # 效价映射
    _positive_labels = {"喜悦", "兴奋", "喜爱", "感动", "撒娇", "调皮"}
    _negative_labels = {"悲伤", "愤怒", "焦虑", "恐惧"}
    if best_label in _positive_labels:
        valence = "positive"
    elif best_label in _negative_labels:
        valence = "negative"
    else:
        valence = "neutral"

    # 强度计算
    intensity = min(0.3 + best_hits * 0.15, 1.0)

    return {
        "primary": best_label,
        "valence": valence,
        "intensity": intensity,
        "pad": pad_from_emotion(best_label, intensity).to_dict(),
    }


def build_emotion_hint(emotion: dict) -> str:
    """根据情绪效价与强度生成给 LLM 的回应语调提示。"""
    valence = emotion.get("valence", "neutral")
    intensity = emotion.get("intensity", 0.0)

    if valence == "positive" and intensity > 0.5:
        return "伙伴现在心情很好，可以轻快地回应"
    if valence == "positive":
        return "伙伴心情不错"
    if valence == "negative" and intensity > 0.5:
        return "伙伴现在情绪比较低落，要温柔地陪伴，不要说教"
    if valence == "negative":
        return "伙伴有些低落，轻轻陪着就好"
    return ""
