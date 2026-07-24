import json
import random
import re
from pathlib import Path
from typing import ClassVar

from loguru import logger

from .emotion_enum import STICKER_FALLBACK, Emotion, is_unified, resolve_emotion


class StickerManager:
    """管理情绪关键词映射与贴纸/表情的选用。"""
    EMOTION_MAP: ClassVar[dict[str, list[str]]] = {
        "happy": [
            "开心", "高兴", "嘻嘻", "哈哈", "太好了", "太棒了", "好耶", "嘿嘿",
            "耶", "棒", "厉害", "好开心", "好高兴", "真棒", "真好",
            "开心～", "嘻嘻～", "嘿嘿～", "好耶！", "太好了！",
            "太开心", "好幸福", "幸福", "满足", "好满足", "满足～",
            "好快乐", "快乐", "乐", "超棒", "超好",
        ],
        "excited": [
            "超开心", "超～开心", "好嗨", "好激动", "兴奋", "期待",
            "太兴奋", "超兴奋", "好兴奋", "激动死了", "嗨翻了",
        ],
        "love": [
            "喜欢", "爱", "心动", "喜爱", "爱慕", "示爱",
            "好喜欢", "超喜欢", "好心动", "好喜欢你",
        ],
        "shy": [
            "害羞", "脸红", "不好意思", "才没有", "才不是", "///",
            "脸红红", "害羞～", "才不是呢", "不要这样说", "讨厌啦",
            "害羞了", "不好意思～", "才不要", "羞", "好羞",
            "捂脸", "脸热", "脸好热",
        ],
        "sad": [
            "难过", "伤心", "呜呜", "555", "好难过", "呜", "可惜",
            "遗憾", "对不起", "抱歉", "呜～", "好伤心", "好可惜", "5555",
            "呜呜呜", "失落", "好失落", "心碎", "好孤独", "孤独",
            "寂寞", "好寂寞", "想哭", "好想哭", "泪", "哭了",
        ],
        "angry": [
            "生气", "哼！", "讨厌", "烦人", "气死", "不理你",
            "好气", "生气了", "讨厌！", "烦！", "气鼓鼓", "哼哼",
            "不要理我", "生气！", "哼", "好烦", "烦死了", "气死我了",
            "暴怒", "好生气", "气人",
        ],
        "surprised": [
            "惊讶", "吃惊", "震惊", "什么！？",
            "吓一跳", "没想到", "想不到", "万万没想到",
        ],
        "confused": [
            "困惑", "疑惑", "不解", "迷茫", "无语",
            "想不明白", "搞不懂", "不明白", "什么意思",
            "听不懂", "看不懂", "不懂",
        ],
        "thinking": [
            "想想", "嗯...", "让我想想", "唔", "这个嘛", "让我看看",
            "唔...", "想一想", "思考", "嗯～", "唔～",
            "让我想想～", "怎么说呢", "让我琢磨", "琢磨",
            "让我思考", "思考一下", "分析一下",
        ],
        "playful": [
            "调皮", "搞怪", "俏皮", "得意", "傲娇", "卖萌",
            "嘿嘿嘿", "咯咯", "嘻嘻嘻", "略略",
        ],
        "moved": [
            "感动", "欣慰", "暖心", "破涕为笑",
            "好感动", "太感动", "暖到了",
        ],
        "neutral": [
            "平静", "淡然", "冷静", "无聊", "困倦", "发呆",
            "嗯", "哦", "好吧", "行吧", "就这样", "随便",
        ],
        "pout": [
            "撒娇", "娇嗔", "嘟嘴", "撅嘴", "耍赖",
            "哼～", "才不要嘛", "人家要", "不理你啦",
        ],
        "fear": [
            "害怕", "恐惧", "好怕", "好恐惧", "好慌",
            "吓", "吓到", "吓死", "可怕", "好可怕", "慌张",
            "惊恐", "惊吓", "颤抖",
        ],
        "anxious": [
            "焦虑", "担心", "紧张", "不安", "忧虑", "慌",
            "好紧张", "好担心", "好不安", "心慌", "忐忑", "心神不宁", "提心吊胆",
        ],
        "curious": [
            "好奇", "咦", "嗯？", "什么呀", "为什么", "咦？",
            "真的吗", "是怎样", "怎么回事", "好奇怪", "咦～",
            "为什么呀", "诶", "诶？", "啊？", "不会吧",
            "竟然", "居然",
        ],
        "greeting": [
            "你好", "早上好", "晚安", "嗨", "早上好呀", "晚安呀", "嗨～",
            "你好呀", "早安", "午安", "你好～", "嗨！", "欢迎",
            "好久不见", "早上好～", "晚安～", "深夜好", "下午好",
            "晚上好", "晚上好呀", "下午好呀", "深夜好呀",
            "回来啦", "回来啦～", "我回来啦", "我回来啦～",
        ],
    }

    EMOTION_EXCLUSIONS: ClassVar[dict[str, list[str]]] = {
        "happy": ["不", "没", "别", "少"],
        "excited": ["不", "没"],
        "love": ["不", "没", "别"],
        "sad": ["不", "别", "不用", "不要", "不会"],
        "angry": ["不", "没", "别"],
        "surprised": ["不"],
        "playful": ["不"],
        "moved": ["不"],
        "pout": ["不"],
        "shy": ["不"],
        "fear": ["不", "没", "别"],
        "anxious": ["不", "没", "别"],
        "neutral": [],
        "confused": [],
        "thinking": [],
        "curious": [],
        "greeting": [],
    }

    EMOTION_PATTERN = re.compile(r'\[emotion:([a-z_]+)\]')

    def __init__(self, sticker_dir: Path | str) -> None:
        """初始化表情包管理器并扫描目录.

        Args:
            sticker_dir: 表情包根目录路径
        """
        self._dir = Path(sticker_dir) if not isinstance(sticker_dir, Path) else sticker_dir
        self._cache: dict[str, list[Path]] = {}
        self._descriptions: dict[str, str] = {}
        self._scan()

    def _scan(self) -> None:
        if not self._dir.exists():
            return
        # 加载描述文件（可选）
        desc_file = self._dir / "descriptions.json"
        if desc_file.exists():
            try:
                self._descriptions = json.loads(desc_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.debug("sticker.description_parse_error", exc_info=True)
                self._descriptions = {}
        for emotion_dir in self._dir.iterdir():
            if emotion_dir.is_dir():
                files = [
                    f for f in emotion_dir.iterdir()
                    if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
                ]
                if files:
                    # 以目录名为准（目录结构即情绪分类），不再根据文件名描述重分类
                    self._cache[emotion_dir.name] = files
        total = sum(len(v) for v in self._cache.values())
        logger.info("sticker.loaded", categories=len(self._cache), total=total)

    def reload(self) -> None:
        """重新扫描表情包目录 (清空缓存)."""
        self._cache.clear()
        self._descriptions.clear()
        self._scan()

    def detect_emotion(self, text: str) -> str:
        """从文本中检测情绪标签或关键词.

        Args:
            text: 输入文本

        Returns:
            检测到的情绪名, 无匹配返回空字符串
        """
        m = self.EMOTION_PATTERN.search(text)
        if m:
            raw_label = m.group(1)
            if is_unified():
                emotion = resolve_emotion(raw_label)
                return STICKER_FALLBACK.get(emotion, "happy")
            if raw_label in self._cache or raw_label in self.EMOTION_MAP:
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
                logger.debug("sticker.emotion_simple_import_error", exc_info=True)
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

        return ""

    def strip_emotion_tag(self, text: str) -> str:
        """移除文本中的情绪/表情包标签.

        支持以下格式：
        - [emotion:xxx] — 标准情绪标签
        - [playful/stickers:xxx] — LLM 幻觉的 sticker 路径标签
        - [happy], [sad], [playful] 等 — 纯情绪词标签
        - [playful/sti — 未闭合的截断标签
        """
        # 标准格式 [emotion:xxx]
        text = re.sub(r'\[emotion:[^\]]*\]', '', text)
        # LLM 幻觉格式 [playful/stickers:xxx /emotions/xxx.png]
        text = re.sub(r'\[\w+/stickers:[^\]]*\]', '', text)
        # 纯情绪词标签 [happy] [sad] [playful] 等（仅匹配已知情绪词）
        text = re.sub(r'\[(?:happy|excited|sad|angry|shy|surprised|fear|neutral|greeting|caring|playful|lonely|thinking)\]', '', text)
        # 未闭合的截断标签 [playful/sti 或 [emotion:ha 等
        text = re.sub(r'\[(?:emotion:|playful/|excited/|happy/|sad/)\S*$', '', text)
        return text.rstrip()

    def should_send(self, text: str, detected_emotion: str = "") -> bool:
        """按概率决定是否发送表情包 (有明确情绪时概率更高).

        Args:
            text: 原始文本
            detected_emotion: 已检测到的情绪, 默认空字符串

        Returns:
            True 表示应发送表情包
        """
        if not self._cache:
            return False
        # 有明确情绪时 100% 发送，neutral/无情绪时 90%
        prob = 1.0 if detected_emotion and detected_emotion != "neutral" else 0.9
        return random.random() < prob

    def get_sticker(self, emotion: str = "") -> Path | None:
        """Alias for pick() — backward compatible"""
        return self.pick(emotion)

    def pick(self, emotion: str | Emotion = "", strict: bool = False) -> Path | None:
        """按情绪随机挑选一张表情包, 无匹配则从全部中随机选.

        Args:
            emotion: 目标情绪 (字符串或 Emotion 枚举), 默认空字符串
            strict: 严格模式 — 指定情绪目录为空时不 fallback 到全部随机，直接返回 None。
                用于子 Agent @ 对话场景：专属表情包目录没有对应情绪分类就不发送。

        Returns:
            表情包文件路径, 无可用时返回 None
        """
        if not self._cache:
            return None
        # 优先直接匹配物理目录名（避免 greeting→happy, curious→confused 等错误降级）
        # 当 LLM 输出 [sticker:greeting] 或 detect_emotion 返回 "greeting" 时，
        # 应直接使用 greeting/ 目录，而非经 STICKER_FALLBACK 降级到 happy
        if emotion and isinstance(emotion, str) and emotion in self._cache:
            return random.choice(self._cache[emotion])
        # 统一模式：Emotion 枚举 → STICKER_FALLBACK 映射
        if emotion and is_unified():
            if isinstance(emotion, Emotion):
                emotion = STICKER_FALLBACK.get(emotion, "happy")
            else:
                resolved = resolve_emotion(str(emotion))
                emotion = STICKER_FALLBACK.get(resolved, "happy")
        if emotion:
            # 优先从物理目录与情绪匹配的文件中选（目录名=情绪名）
            if self._cache.get(emotion):
                return random.choice(self._cache[emotion])
            # 指定情绪目录不存在或为空
            if strict:
                # 严格模式：不 fallback，没有对应情绪的表情就不发送
                logger.debug(f"sticker.strict_no_match emotion={emotion} (dir empty/missing)")
                return None
            # fallback 到全部随机选
            logger.debug(f"sticker.emotion_dir_empty fallback_to_all emotion={emotion}")
        all_stickers = [s for v in self._cache.values() for s in v]
        return random.choice(all_stickers) if all_stickers else None

    @property
    def available(self) -> bool:
        """返回是否已加载任何表情包."""
        return bool(self._cache)

    def pick_by_text(self, text: str, detected_emotion: str = "") -> Path | None:
        """根据文本内容挑选表情包 (优先用已检测情绪).

        Args:
            text: 原始文本
            detected_emotion: 已检测情绪, 默认空字符串

        Returns:
            表情包路径, 无可用返回 None
        """
        target_emotion = detected_emotion or self.detect_emotion(text)
        if not target_emotion:
            target_emotion = "neutral"
        return self.pick(target_emotion)

    def get_description(self, filepath: Path) -> str:
        """获取表情包描述：优先 descriptions.json，否则从文件名提取.

        文件名提取规则：去掉 emotion 前缀（如 happy_）和扩展名，分隔符替换为空格。
        例如 happy_闭眼满足微笑.jpg → "闭眼满足微笑"
        """
        name = filepath.name
        if name in self._descriptions:
            return self._descriptions[name]
        # 从文件名提取：去掉 前缀_ 和扩展名
        stem = filepath.stem
        # 去掉 emotion 前缀（如 happy_, sad_, neutral_）
        if "_" in stem:
            parts = stem.split("_", 1)
            if parts[0] in self._cache or parts[0] in self.EMOTION_MAP:
                return parts[1].replace("_", " ").replace("-", " ")
        return stem.replace("_", " ").replace("-", " ")

    def list_stickers(self, emotion: str = "") -> list[dict]:
        """列出可用表情包及描述.

        Args:
            emotion: 指定情绪分类，为空则列出全部

        Returns:
            [{"name": 文件名, "description": 描述, "emotion": 情绪分类}, ...]
        """
        result = []
        dirs = {emotion: self._cache.get(emotion, [])} if emotion else self._cache
        for emo, files in dirs.items():
            for f in files:
                result.append({
                    "name": f.name,
                    "description": self.get_description(f),
                    "emotion": emo,
                })
        return result

    def pick_by_name(self, filename: str) -> Path | None:
        """按文件名精确选择表情包.

        Args:
            filename: 表情包文件名（含扩展名）

        Returns:
            表情包路径, 未找到返回 None
        """
        for files in self._cache.values():
            for f in files:
                if f.name == filename:
                    return f
        return None
