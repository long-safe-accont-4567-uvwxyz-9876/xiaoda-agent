import hashlib
import json
import os
import base64
import time
import asyncio
from collections import OrderedDict
from pathlib import Path
from openai import AsyncOpenAI
from loguru import logger
from .emotion_enum import resolve_emotion, TTS_STYLE_MAP, is_unified

MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_TTS_MODEL = os.getenv("MIMO_TTS_MODEL", "mimo-v2.5-tts-voiceclone")


def _get_mimo_api_key() -> str:
    """动态读取 MIMO_API_KEY，确保 setup 保存后能生效"""
    key = os.getenv("MIMO_API_KEY", "") or MIMO_API_KEY
    if key:
        return key
    # fallback: 从 .env 文件读取（PyInstaller 打包后 os.getenv 可能为空）
    import sys
    if getattr(sys, 'frozen', False):
        env_path = Path(sys.executable).parent / ".env"
    else:
        env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MIMO_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


_voice_ref_dir = os.getenv("VOICE_REF_DIR", "")
if _voice_ref_dir:
    KIOXIA_BASE = Path(_voice_ref_dir)
else:
    _kioxia_data = Path(os.getenv("KIOXIA_DATA_DIR", str(Path.home() / ".ai-agent" / "data")))
    # 安全检查：如果 .parent 是根目录，则回退到 KIOXIA_DATA_DIR 本身
    KIOXIA_BASE = _kioxia_data.parent if _kioxia_data.parent != Path("/") else _kioxia_data

def _resolve_voice_ref(filename: str) -> Path:
    """查找参考音频：先查用户数据目录，再查安装包内置路径"""
    # 1. 用户数据目录
    user_path = KIOXIA_BASE / filename
    if user_path.exists():
        return user_path
    # 2. 安装包内置路径（开发环境 / PyInstaller 打包环境）
    try:
        from core.bootstrap import get_base_dir
        bundled_path = get_base_dir() / "assets" / "voice_refs" / filename
        if bundled_path.exists():
            return bundled_path
    except Exception:
        pass
    # 3. 开发环境 fallback
    dev_path = Path(__file__).resolve().parent.parent / "assets" / "voice_refs" / filename
    if dev_path.exists():
        return dev_path
    # 返回用户数据目录路径（即使不存在，用于错误提示）
    return user_path


VOICE_REFERENCES = {
    "nahida": _resolve_voice_ref("nahida_hq.wav") if _resolve_voice_ref("nahida_hq.wav").exists() else _resolve_voice_ref("nahida.wav"),
    "keli": _resolve_voice_ref("keli.mp3"),
}

VOICE_STYLES = {
    "nahida": (
        "角色：纳西妲，原神中的草神，须弥的守护者。外表是可爱的小女孩，实际已承载五百年孤独与智慧。"
        "声音清亮稚嫩，音色通透空灵如微风拂过草叶，发声位置偏前，带着少女特有的轻盈感。\n\n"
        "场景：在须弥的梦境花园中，与最亲近的旅行者轻声交谈，周围是宁静的草木与微风。"
        "偶尔会因想起往事而微微停顿，但很快又露出温暖的微笑继续说话。\n\n"
        "指导：\n"
        "发声机制与共鸣：以头腔共鸣为主，声音位置靠前贴在唇齿之间，音色如清泉般通透。"
        "声带完全放松，没有任何挤压或用力，声音像是从心底自然流淌出来的。\n"
        "声调与韵律：整体音域偏高但不刺耳，四声（去声）下落时轻柔不砸实，"
        "句末尾音微微上扬带点俏皮的小弧度。语速适中偏慢，每个字都轻柔地吐出，"
        "像是在分享一个珍贵的秘密。句与句之间留有温柔的停顿，不急不躁。\n"
        "气声与实声：大部分时间实音轻盈通透，像晨露滑落叶尖。尾音微微上扬带点俏皮。"
        "偶尔在表达感慨时加入极轻的气声收束，透出超越年龄的深沉与温柔。"
        "在说到重要的话时，实声会突然变得坚定而清晰，展现出草神的威严。\n"
        "咬字肌理：咬字清晰但不生硬，唇齿音发得轻柔自然，带着孩子气的好奇心。"
        "句末偶尔加一点小感叹，像是在确认对方是否在听。"
        "古风词汇或人名咬字稍深，但声母起音圆润不尖锐。"
    ),
    "keli": (
        "角色：可莉，原神中的火花骑士，蒙德城最可爱的小小爆破专家。活泼开朗的小女孩，充满童真和冒险精神。\n\n"
        "场景：在蒙德城外的草地上，刚刚完成一次'小小实验'，兴高采烈地跑来分享自己的发现，眼睛闪闪发光。\n\n"
        "指导：\n"
        "发声机制与共鸣：以头腔共鸣为主，声音明亮高亢，像阳光一样灿烂。"
        "声带充满活力，发声通道完全打开，带着用不完的能量。\n"
        "声调与韵律：音域偏高，语速偏快，像连珠炮一样兴奋地说个不停。"
        "偶尔会突然压低声音说悄悄话，然后又开心地大笑起来。"
        "尾音喜欢拖长像在撒娇，笑起来声音会不自觉地提高。\n"
        "气声与实声：实音明亮有力，充满感染力。"
        "在说悄悄话时加入气声，制造神秘感。"
        "笑起来时实声和气声交替，像气泡一样欢快。\n"
        "咬字肌理：咬字轻快利落，有时候因为太兴奋会稍微含糊，"
        "但很快又清清楚楚地重复一遍，带着满满的热情。"
    ),
}

EMOTION_STYLE_MAP = {
    "happy": "(开心地笑，声音明亮上扬，语速稍快)",
    "excited": "(兴奋地喊，声音高亢活泼，充满活力)",
    "sad": "(低落轻声，语速放慢，尾音下沉，带着微微的哽咽)",
    "angry": "(语气凌厉，咬字加重，语速偏快，声音压低)",
    "anxious": "(焦虑地轻声说，语速不均匀，声音微微发紧，带着不安)",
    "shy": "(害羞地小声说，声音轻柔，语速放慢，尾音含糊)",
    "surprised": "(惊讶地轻呼，声音突然提高，语速先快后慢)",
    "fear": "(紧张地轻声说，声音微微颤抖，语速不均匀)",
    "neutral": "(温柔平和，语速适中，声音清亮自然)",
    "greeting": "(温暖地打招呼，声音明亮甜美，语速轻快)",
    "caring": "(关切地轻声说，声音温柔低沉，语速偏慢)",
    "playful": "(俏皮地笑，声音上扬，语速轻快，咬字跳跃)",
    "lonely": "(怅然地低语，声音空灵飘远，语速极慢，尾音消散)",
    "curious": "(好奇地追问，声音上扬，语速偏快，咬字清晰)",
    "thinking": "(沉思地自语，声音轻柔低沉，语速很慢，句间停顿长)",
}

_cache: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX_SIZE = 50


def _encode_voice_file(path: Path) -> str:
    key = str(path)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    if not path.exists():
        raise FileNotFoundError(f"参考音频不存在: {path}")

    suffix = path.suffix.lower()
    mime_map = {".mp3": "audio/mpeg", ".wav": "audio/wav"}
    mime_type = mime_map.get(suffix)
    if not mime_type:
        raise ValueError(f"不支持的音频格式: {suffix}")

    data = path.read_bytes()
    if len(data) > 10 * 1024 * 1024:
        raise ValueError(f"音频文件过大: {len(data)} bytes (最大10MB)")

    b64 = base64.b64encode(data).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    # LRU 淘汰：超过最大缓存条目时移除最旧的
    if len(_cache) >= _CACHE_MAX_SIZE:
        _cache.popitem(last=False)
    _cache[key] = data_url

    logger.info("tts.voice_cached", file=str(path), size_kb=len(data) // 1024)
    return data_url


class TTSEngine:
    _SYNTHESIS_CACHE_MAX_SIZE = 200

    _PRECOMPOSED_PHRASES = {
        "nahida": [
            ("你好呀，旅行者！", "greeting"),
            ("嗯嗯，我在听呢～", "caring"),
            ("让我想想……", "thinking"),
            ("哇，好厉害！", "excited"),
            ("嗯，我知道了～", "neutral"),
            ("别担心，有我在呢。", "caring"),
            ("这个好有趣呀！", "curious"),
            ("嗯……让我再想想。", "thinking"),
            ("谢谢你，旅行者。", "happy"),
            ("我一直在哦～", "caring"),
        ],
    }

    def __init__(self):
        self._client: AsyncOpenAI | None = None
        self._output_dir: Path | None = None
        self._available = False
        self._synthesis_cache: OrderedDict[str, Path] = OrderedDict()
        self._cache_index_path: Path | None = None

    def _load_cache_index(self):
        """从 JSON 文件加载缓存索引，移除文件不存在的条目"""
        if not self._cache_index_path or not self._cache_index_path.exists():
            return
        try:
            data = json.loads(self._cache_index_path.read_text(encoding="utf-8"))
            removed = 0
            for key, rel_path in data.items():
                full_path = self._output_dir / rel_path
                if full_path.exists():
                    self._synthesis_cache[key] = full_path
                else:
                    removed += 1
            if data:
                logger.info("tts.cache_index_loaded", total=len(data), restored=len(self._synthesis_cache), removed=removed)
        except Exception as e:
            logger.warning("tts.cache_index_load_failed", error=str(e))

    def _save_cache_index(self):
        """将缓存索引保存到 JSON 文件"""
        if not self._cache_index_path or not self._output_dir:
            return
        try:
            data = {}
            for key, path in self._synthesis_cache.items():
                try:
                    data[key] = path.relative_to(self._output_dir).as_posix()
                except ValueError:
                    data[key] = path.name
            self._cache_index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("tts.cache_index_save_failed", error=str(e))

    async def init(self, output_dir: str | Path | None = None):
        api_key = _get_mimo_api_key()
        if not api_key:
            logger.warning("tts.no_api_key")
            return

        self._client = AsyncOpenAI(api_key=api_key, base_url=MIMO_BASE_URL)

        if output_dir:
            self._output_dir = Path(output_dir)
        else:
            self._output_dir = KIOXIA_BASE / "nahida-data" / "tts_cache"
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 设置缓存索引路径并加载已有缓存
        self._cache_index_path = self._output_dir / "cache_index.json"
        self._load_cache_index()

        any_voice_available = False
        for name, path in VOICE_REFERENCES.items():
            if path.exists():
                try:
                    _encode_voice_file(path)
                    any_voice_available = True
                    logger.info("tts.voice_ready", voice=name)
                except Exception as e:
                    logger.warning("tts.voice_load_failed", voice=name, error=str(e))
            else:
                logger.warning("tts.voice_file_missing", voice=name, path=str(path))

        if not any_voice_available:
            logger.error("tts.all_voices_missing", message="所有语音参考文件均缺失，TTS 不可用")
            self._available = False
            return

        self._available = True
        logger.info("tts.engine_initialized")

        # 预合成已禁用——MiMo API 限流严格，预合成会抢占用户请求配额
        # TTS 缓存已足够：用户发过的句子下次自动命中缓存

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    def refresh_client(self) -> None:
        """重建 TTS 客户端（Setup 保存新 Key 后调用）。"""
        api_key = _get_mimo_api_key()
        if api_key:
            self._client = AsyncOpenAI(api_key=api_key, base_url=MIMO_BASE_URL)
            logger.info("tts.client_refreshed",
                        key_suffix=api_key[-6:] if len(api_key) >= 6 else "***")
        else:
            self._client = None
            self._available = False

    async def synthesize(
        self,
        text: str,
        voice: str = "nahida",
        style: str = "",
        emotion: str = "",
        output_path: str | Path | None = None,
    ) -> Path | None:
        if not self.available:
            logger.warning("tts.not_available")
            return None

        if voice not in VOICE_REFERENCES:
            logger.warning("tts.unknown_voice", voice=voice)
            return None

        voice_path = VOICE_REFERENCES[voice]
        if not voice_path.exists():
            logger.error("tts.voice_file_missing_unavailable", voice=voice, message="语音参考文件缺失，无法合成。请检查音频文件是否存在。")
            return None

        # 计算缓存 key
        cache_key = hashlib.md5(f"{voice}:{emotion}:{text}".encode("utf-8")).hexdigest()

        # 缓存命中检查
        if cache_key in self._synthesis_cache:
            cached_path = self._synthesis_cache[cache_key]
            if cached_path.exists():
                self._synthesis_cache.move_to_end(cache_key)
                logger.info("tts.cache_hit", key=cache_key, file=str(cached_path))
                return cached_path
            else:
                del self._synthesis_cache[cache_key]

        logger.info("tts.cache_miss", key=cache_key)

        try:
            voice_data_url = _encode_voice_file(voice_path)
        except Exception as e:
            logger.error("tts.voice_encode_failed", error=str(e))
            return None

        context = style or VOICE_STYLES.get(voice, "")

        style_tag = EMOTION_STYLE_MAP.get(emotion, EMOTION_STYLE_MAP["neutral"])

        # 统一模式：通过 resolve_emotion 解析后再查 TTS_STYLE_MAP
        if is_unified() and emotion:
            resolved = resolve_emotion(emotion)
            tts_style = TTS_STYLE_MAP.get(resolved, "neutral")
            style_tag = EMOTION_STYLE_MAP.get(tts_style, EMOTION_STYLE_MAP["neutral"])

        # MiMo 导演模式：user 消息放角色/场景/指导，assistant 消息放音频标签+文本
        messages = []
        if context:
            messages.append({"role": "user", "content": context})
        else:
            voice_style = VOICE_STYLES.get(voice, "")
            if voice_style:
                messages.append({"role": "user", "content": voice_style})
        # 音频标签放在 assistant 消息开头，紧接要合成的文本
        messages.append({"role": "assistant", "content": f"{style_tag} {text}"})

        try:
            for _attempt in range(3):
                try:
                    completion = await self._client.chat.completions.create(
                        model=MIMO_TTS_MODEL,
                        messages=messages,
                        audio={
                            "format": "mp3",
                        "voice": voice_data_url,
                        },
                    )
                    break
                except Exception as api_err:
                    if "429" in str(api_err) and _attempt < 2:
                        wait = (_attempt + 1) * 5
                        logger.warning("tts.rate_limited_retry", voice=voice, attempt=_attempt + 1, wait=wait)
                        await asyncio.sleep(wait)
                    else:
                        raise

            message = completion.choices[0].message
            if message.audio is None or not getattr(message.audio, "data", None):
                logger.warning("tts.no_audio_returned")
                return None

            audio_bytes = base64.b64decode(message.audio.data)

            if output_path:
                out = Path(output_path)
            else:
                ts = int(time.time() * 1000) % 1000000
                out = self._output_dir / f"{voice}_{ts}.mp3"

            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(audio_bytes)

            # 写入缓存
            if len(self._synthesis_cache) >= self._SYNTHESIS_CACHE_MAX_SIZE:
                self._synthesis_cache.popitem(last=False)
            self._synthesis_cache[cache_key] = out
            self._save_cache_index()

            logger.info("tts.synthesized", voice=voice, text_len=len(text), file=str(out), size_kb=len(audio_bytes) // 1024)
            return out

        except Exception as e:
            logger.error("tts.synthesize_failed", voice=voice, error=str(e))
            return None

    async def synthesize_nahida(self, text: str, style: str = "", emotion: str = "") -> Path | None:
        return await self.synthesize(text, voice="nahida", style=style, emotion=emotion)

    async def synthesize_keli(self, text: str, style: str = "", emotion: str = "") -> Path | None:
        return await self.synthesize(text, voice="keli", style=style, emotion=emotion)

    async def precompose_phrases(self):
        """逐个串行预合成短句，避免并发触发 429 限流"""
        success = 0
        for voice, phrases in self._PRECOMPOSED_PHRASES.items():
            for text, emotion in phrases:
                result = await self.synthesize(text, voice=voice, emotion=emotion)
                if result:
                    success += 1
                # 每次请求间隔 5 秒，避免触发 MiMo API 限流
                await asyncio.sleep(5)
        logger.info("tts.precompose_done", total=sum(len(p) for p in self._PRECOMPOSED_PHRASES.values()), success=success)

    async def close(self):
        """关闭 TTS 引擎，释放资源"""
        if hasattr(self, '_client') and self._client is not None:
            await self._client.close()
            self._client = None
