import re
import random
from pathlib import Path
from loguru import logger


class StickerManager:
    EMOTION_MAP = {
        "happy": [
            "开心", "高兴", "嘻嘻", "哈哈", "太好了", "太棒了", "好耶", "嘿嘿",
            "耶", "棒", "厉害", "好开心", "好高兴", "真棒", "真好", "喜欢",
            "好喜欢", "开心～", "嘻嘻～", "嘿嘿～", "好耶！", "太好了！",
            "超开心", "超～开心", "好嗨", "好激动", "兴奋", "期待",
            "太开心", "好幸福", "幸福", "满足", "好满足", "满足～",
            "好快乐", "快乐", "乐", "超棒", "超好", "超喜欢",
        ],
        "sad": [
            "难过", "伤心", "呜呜", "555", "好难过", "呜", "可惜",
            "遗憾", "对不起", "抱歉", "呜～", "好伤心", "好可惜", "5555",
            "呜呜呜", "失落", "好失落", "心碎", "好孤独", "孤独",
            "寂寞", "好寂寞", "想哭", "好想哭", "泪", "哭了",
        ],
        "shy": [
            "害羞", "脸红", "不好意思", "才没有", "才不是", "///",
            "脸红红", "害羞～", "才不是呢", "不要这样说", "讨厌啦",
            "害羞了", "不好意思～", "才不要", "羞", "好羞",
            "捂脸", "脸热", "脸好热",
        ],
        "angry": [
            "生气", "哼！", "讨厌", "烦人", "气死", "不理你",
            "好气", "生气了", "讨厌！", "烦！", "气鼓鼓", "哼哼",
            "不要理我", "生气！", "哼", "好烦", "烦死了", "气死我了",
            "暴怒", "好生气", "气人",
        ],
        "curious": [
            "好奇", "咦", "嗯？", "什么呀", "为什么", "咦？",
            "真的吗", "是怎样", "怎么回事", "好奇怪", "咦～", "嗯？",
            "为什么呀", "诶", "诶？", "啊？", "什么！？", "不会吧",
            "竟然", "居然",
        ],
        "greeting": [
            "你好", "早上好", "晚安", "嗨", "早上好呀", "晚安呀", "嗨～",
            "你好呀", "早安", "午安", "你好～", "嗨！", "欢迎",
            "好久不见", "早上好～", "晚安～", "深夜好", "下午好",
            "晚上好", "晚上好呀", "下午好呀", "深夜好呀",
            "回来啦", "回来啦～", "我回来啦", "我回来啦～",
        ],
        "thinking": [
            "想想", "嗯...", "让我想想", "唔", "这个嘛", "让我看看",
            "唔...", "想一想", "思考", "嗯～", "唔～",
            "让我想想～", "怎么说呢", "让我琢磨", "琢磨",
            "让我思考", "思考一下", "分析一下",
        ],
    }

    EMOTION_EXCLUSIONS = {
        "happy": ["不", "没", "别", "少"],
        "sad": ["不", "别", "不用", "不要", "不会"],
        "angry": ["不", "没", "别"],
        "shy": ["不"],
    }

    EMOTION_PATTERN = re.compile(r'\[emotion:(\w+)\]')

    FILENAME_KEYWORDS = {
        "happy": ["开心", "满足", "微笑", "比耶", "卖萌", "亮晶晶", "脸红", "期待", "兴奋", "大笑", "星星眼", "玫瑰", "叼玫瑰"],
        "sad": ["苦笑", "晕眩", "委屈", "无奈", "墨镜", "含泪", "泪光", "流泪", "哭泣", "暗淡", "阴沉"],
        "angry": ["愤怒", "激光", "暴怒", "生气", "流汗", "困惑", "问号", "惊讶大眼"],
        "shy": ["温柔", "害羞", "小嘴", "惊恐", "害怕", "闭眼", "吐舌", "委屈难过"],
        "curious": ["爱心眼", "喜欢", "兴奋", "惊讶张嘴", "泪汪汪", "惊讶好奇"],
        "greeting": ["惊讶", "好奇", "困惑无语", "无聊", "困倦", "惊喜", "示爱"],
        "thinking": ["疑惑", "大大的疑惑", "温柔微笑", "哭泣微笑"],
    }

    def __init__(self, sticker_dir: Path):
        self._dir = sticker_dir
        self._cache: dict[str, list[Path]] = {}
        self._scan()

    def _scan(self):
        if not self._dir.exists():
            return
        for emotion_dir in self._dir.iterdir():
            if emotion_dir.is_dir():
                files = [
                    f for f in emotion_dir.iterdir()
                    if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
                ]
                if files:
                    self._cache[emotion_dir.name] = files
        total = sum(len(v) for v in self._cache.values())
        logger.info(f"sticker.loaded", categories=len(self._cache), total=total)

    def reload(self):
        self._cache.clear()
        self._scan()

    def detect_emotion(self, text: str) -> str:
        m = self.EMOTION_PATTERN.search(text)
        if m:
            emotion = m.group(1)
            if emotion in self._cache or emotion in self.EMOTION_MAP:
                return emotion

        for emotion, keywords in self.EMOTION_MAP.items():
            exclusions = self.EMOTION_EXCLUSIONS.get(emotion, [])
            for kw in keywords:
                if kw in text:
                    idx = text.index(kw)
                    prefix = text[max(0, idx - 2):idx]
                    if any(ex in prefix for ex in exclusions):
                        continue
                    return emotion

        filename_match = self._detect_from_filename(text)
        if filename_match:
            return filename_match

        return ""

    def _detect_from_filename(self, text: str) -> str:
        best_emotion = ""
        best_score = 0
        for emotion, keywords in self.FILENAME_KEYWORDS.items():
            score = 0
            for kw in keywords:
                if kw in text:
                    idx = text.index(kw)
                    prefix = text[max(0, idx - 2):idx]
                    exclusions = self.EMOTION_EXCLUSIONS.get(emotion, [])
                    if any(ex in prefix for ex in exclusions):
                        continue
                    score += 1
            if score > best_score:
                best_score = score
                best_emotion = emotion
        return best_emotion if best_score >= 1 else ""

    def strip_emotion_tag(self, text: str) -> str:
        return self.EMOTION_PATTERN.sub('', text).rstrip()

    def should_send(self, text: str, detected_emotion: str = "") -> bool:
        if not self._cache:
            return False
        if detected_emotion:
            prob = 0.7
        else:
            prob = 0.40
        return random.random() < prob

    def pick(self, emotion: str = "") -> Path | None:
        if not self._cache:
            return None
        if emotion and emotion in self._cache:
            candidates = self._cache[emotion]
            return random.choice(candidates)
        all_stickers = [s for v in self._cache.values() for s in v]
        return random.choice(all_stickers) if all_stickers else None

    @property
    def available(self) -> bool:
        return bool(self._cache)

    def pick_by_text(self, text: str, detected_emotion: str = "") -> Path | None:
        target_emotion = detected_emotion or self.detect_emotion(text)
        if not target_emotion:
            filename_match = self._detect_from_filename(text)
            if filename_match:
                target_emotion = filename_match
        return self.pick(target_emotion)
