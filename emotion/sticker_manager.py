import re
import random
from pathlib import Path
from loguru import logger
from .emotion_enum import Emotion, resolve_emotion, STICKER_FALLBACK, is_unified


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
        "fear": [
            "焦虑", "担心", "害怕", "紧张", "不安", "恐惧", "慌",
            "好怕", "好紧张", "好担心", "好不安", "好恐惧", "好慌",
            "吓", "吓到", "吓死", "可怕", "好可怕", "慌张",
            "心慌", "忐忑", "心神不宁", "提心吊胆",
        ],
    }

    EMOTION_EXCLUSIONS = {
        "happy": ["不", "没", "别", "少"],
        "sad": ["不", "别", "不用", "不要", "不会"],
        "angry": ["不", "没", "别"],
        "shy": ["不"],
        "fear": ["不", "没", "别"],
    }

    EMOTION_PATTERN = re.compile(r'\[emotion:([a-z_]+)\]')

    # 文件名描述关键词到情绪的映射（统一到 EMOTION_ALIASES，消除三套表不一致）
    _DESC_EMOTION_MAP = {
        # happy
        "开心": "happy", "满足": "happy", "微笑": "happy", "比耶": "happy",
        "卖萌": "happy", "亮晶晶": "happy", "期待": "happy", "兴奋": "happy",
        "大笑": "happy", "星星眼": "happy", "玫瑰": "happy", "叼玫瑰": "happy",
        "酷笑": "happy", "温柔微笑": "happy", "惊喜": "happy", "示爱": "happy",
        "温柔": "happy",
        # sad
        "苦笑": "sad", "晕眩": "sad", "委屈": "sad", "无奈": "sad",
        "含泪": "sad", "泪光": "sad", "流泪": "sad", "哭泣": "sad",
        "暗淡": "sad", "阴沉": "sad", "难过": "sad",
        # angry
        "愤怒": "angry", "激光": "angry", "暴怒": "angry", "生气": "angry",
        # shy
        "害羞": "shy", "小嘴": "shy", "吐舌": "shy",
        # fear (含 anxious 降级)
        "恐惧": "fear", "害怕": "fear", "惊恐": "fear",
        "焦虑": "fear", "紧张": "fear", "不安": "fear",
        "慌张": "fear", "担心": "fear", "惊吓": "fear", "颤抖": "fear",
        # curious
        "爱心眼": "curious", "喜欢": "curious", "惊讶张嘴": "curious",
        "泪汪汪": "curious", "惊讶好奇": "curious", "惊讶": "curious",
        "疑惑": "curious",  # 与 EMOTION_ALIASES 一致：疑惑→curious
        # thinking
        "困惑": "thinking", "问号": "thinking",
        "无聊": "thinking", "困倦": "thinking", "哭泣微笑": "thinking",
    }

    def __init__(self, sticker_dir: Path | str):
        self._dir = Path(sticker_dir) if not isinstance(sticker_dir, Path) else sticker_dir
        self._cache: dict[str, list[Path]] = {}
        self._emotion_cache: dict[str, list[Path]] = {}
        self._scan()

    def _classify_by_desc(self, filename_stem: str) -> str:
        """根据文件名描述部分判断表情包的实际情绪类别。

        文件名格式: {目录名}_{实际情绪描述}.jpg
        例如: angry_开心流汗.jpg -> 描述是"开心流汗"，匹配到 happy
        """
        if "_" in filename_stem:
            desc = filename_stem.split("_", 1)[1]
        else:
            desc = filename_stem

        # 按关键词长度降序匹配，优先匹配更长的关键词
        best_emotion = ""
        best_len = 0
        for keyword, emotion in self._DESC_EMOTION_MAP.items():
            if keyword in desc and len(keyword) > best_len:
                best_emotion = emotion
                best_len = len(keyword)
        return best_emotion

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
                    # 根据文件名描述重新归类到正确的情绪
                    for f in files:
                        classified_emotion = self._classify_by_desc(f.stem)
                        if classified_emotion:
                            self._emotion_cache.setdefault(classified_emotion, []).append(f)
                        else:
                            # 无法从描述判断时，使用目录名
                            self._emotion_cache.setdefault(emotion_dir.name, []).append(f)
        total = sum(len(v) for v in self._cache.values())
        emotion_total = sum(len(v) for v in self._emotion_cache.values())
        logger.info(f"sticker.loaded", categories=len(self._cache), total=total, emotion_classified=emotion_total)

    def reload(self):
        self._cache.clear()
        self._emotion_cache.clear()
        self._scan()

    def detect_emotion(self, text: str) -> str:
        m = self.EMOTION_PATTERN.search(text)
        if m:
            raw_label = m.group(1)
            if is_unified():
                emotion = resolve_emotion(raw_label)
                return STICKER_FALLBACK.get(emotion, "happy")
            if raw_label in self._cache or raw_label in self.EMOTION_MAP or raw_label in self._emotion_cache:
                return raw_label

        if is_unified():
            # 统一模式：用 emotion_simple 检测 → resolve_emotion → STICKER_FALLBACK
            try:
                from .emotion_simple import detect_emotion as _detect
                result = _detect(text)
                cn_label = result.get("primary", "平静") if isinstance(result, dict) else str(result)
                emotion = resolve_emotion(cn_label)
                return STICKER_FALLBACK.get(emotion, "happy")
            except Exception:
                return ""

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
        # 从 _DESC_EMOTION_MAP 匹配
        for keyword, emotion in self._DESC_EMOTION_MAP.items():
            if keyword in text:
                idx = text.index(keyword)
                prefix = text[max(0, idx - 2):idx]
                exclusions = self.EMOTION_EXCLUSIONS.get(emotion, [])
                if any(ex in prefix for ex in exclusions):
                    continue
                score = len(keyword)  # 长关键词权重更高
                if score > best_score:
                    best_score = score
                    best_emotion = emotion
        return best_emotion if best_score >= 1 else ""

    def strip_emotion_tag(self, text: str) -> str:
        return re.sub(r'\[emotion:[^\]]*\]', '', text).rstrip()

    def should_send(self, text: str, detected_emotion: str = "") -> bool:
        if not self._cache:
            return False
        # Bug fix: 有明确情绪时提高发送概率，无情绪时降低
        if detected_emotion:
            prob = 0.85
        else:
            prob = 0.30
        return random.random() < prob

    def get_sticker(self, emotion: str = "") -> Path | None:
        """Alias for pick() — backward compatible"""
        return self.pick(emotion)

    def pick(self, emotion: str | Emotion = "") -> Path | None:
        if not self._cache:
            return None
        # 统一模式：Emotion 枚举 → STICKER_FALLBACK 映射
        if emotion and is_unified():
            if isinstance(emotion, Emotion):
                emotion = STICKER_FALLBACK.get(emotion, "happy")
            else:
                resolved = resolve_emotion(str(emotion))
                emotion = STICKER_FALLBACK.get(resolved, "happy")
        if emotion:
            # Bug fix: 优先从物理目录与情绪匹配的文件中选（目录名=情绪名）
            if emotion in self._cache:
                candidates = self._cache[emotion]
                return random.choice(candidates)
            # 回退：从描述归类的情绪缓存中选取
            if emotion in self._emotion_cache:
                candidates = self._emotion_cache[emotion]
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
