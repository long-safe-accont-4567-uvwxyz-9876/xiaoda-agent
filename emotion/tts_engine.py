import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, ClassVar

from loguru import logger
from openai import AsyncOpenAI

from config import get_agent_display_name

from .emotion_enum import TTS_STYLE_MAP, is_unified, resolve_emotion

MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_TTS_MODEL = os.getenv("MIMO_TTS_MODEL", "mimo-v2.5-tts-voiceclone")


def _get_mimo_api_key() -> str:
    """动态读取 MIMO_API_KEY，确保 setup 保存后能生效"""
    key = os.getenv("MIMO_API_KEY", "") or MIMO_API_KEY
    if key:
        return key
    # fallback: 从 .env 文件读取（PyInstaller 打包后 os.getenv 可能为空）
    from utils.env_reader import read_env_key
    return read_env_key("MIMO_API_KEY")


_voice_ref_dir = os.getenv("VOICE_REF_DIR", "")
if _voice_ref_dir:
    KIOXIA_BASE = Path(_voice_ref_dir)
else:
    _kioxia_data = Path(os.getenv("KIOXIA_DATA_DIR", str(Path.home() / ".ai-agent" / "data")))
    # 安全检查：如果 .parent 是根目录，则回退到 KIOXIA_DATA_DIR 本身
    KIOXIA_BASE = _kioxia_data.parent if _kioxia_data.parent != Path("/") else _kioxia_data

def _resolve_voice_ref(filename: str) -> Path:
    """查找参考音频：先查用户数据目录（新结构 voice_refs/agent/ 和旧结构），再查安装包内置路径"""
    stem = filename.rsplit(".", 1)[0].lower()
    agent_name = None
    # 改名遗留兼容：nahida → xiaoda（纳西妲→小妲改名前的旧文件名）
    _prefix_to_agent = {
        "xiaoda": "xiaoda",
        "nahida": "xiaoda",  # 兼容旧文件名 nahida.wav / nahida_hq.wav
        "xiaoli": "xiaoli",
        "xiaoke": "xiaoke",
        "xiaolian": "xiaolian",
        "xiaolang": "xiaolang",
    }
    for prefix, agent in _prefix_to_agent.items():
        if stem.startswith(prefix):
            agent_name = agent
            break
    # 获取与 config.py 一致的参考音频目录
    try:
        from config import VOICE_REF_DIR as _ref_dir
    except ImportError:
        _ref_dir = KIOXIA_BASE / "voice_refs"
    # 1. 用户数据目录 — 新结构: voice_refs/{agent}/filename
    if agent_name:
        new_path = _ref_dir / agent_name / filename
        if new_path.exists():
            return new_path
    # 2. 用户数据目录 — 旧结构: data 根目录直接存放
    _data_base = _ref_dir.parent
    user_path = _data_base / filename
    if user_path.exists():
        return user_path
    # 3. 用户数据目录 — 旧结构: .ai-agent 根目录（tts_engine 历史路径）
    legacy_path = KIOXIA_BASE / filename
    if legacy_path.exists() and legacy_path != user_path:
        return legacy_path
    # 4. 安装包内置路径（开发环境 / PyInstaller 打包环境）
    import sys
    try:
        if getattr(sys, 'frozen', False):
            _base_dir = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        else:
            _base_dir = Path(__file__).resolve().parent.parent
        bundled_path = _base_dir / "assets" / "voice_refs" / filename
        if bundled_path.exists():
            return bundled_path
    except Exception:
        logger.debug("tts.voice_ref_path_error", exc_info=True)
    # 5. 开发环境 fallback
    dev_path = Path(__file__).resolve().parent.parent / "assets" / "voice_refs" / filename
    if dev_path.exists():
        return dev_path
    # 返回新结构路径（即使不存在，用于错误提示）
    if agent_name:
        return _ref_dir / agent_name / filename
    return user_path


def _resolve_xiaoda_voice() -> Path:
    """智能查找 xiaoda 参考音频：优先 xiaoda_hq.wav，回退 nahida_hq.wav / xiaoda.wav / nahida.wav。"""
    _candidates = [
        "xiaoda_hq.wav", "nahida_hq.wav",  # HQ 版本优先
        "xiaoda.wav", "nahida.wav",         # 标准版本回退
    ]
    for _name in _candidates:
        _p = _resolve_voice_ref(_name)
        if _p.exists():
            return _p
    # 全部缺失时返回 xiaoda_hq.wav 路径（用于错误提示）
    return _resolve_voice_ref("xiaoda_hq.wav")


VOICE_REFERENCES = {
    "xiaoda": _resolve_xiaoda_voice(),
    "xiaoli": _resolve_voice_ref("xiaoli.mp3"),
}

def _get_voice_upload_dir() -> Path:
    """延迟解析参考音频根目录，避免 config 模块未初始化时路径不一致。"""
    try:
        from config import VOICE_REF_DIR
        return VOICE_REF_DIR
    except ImportError:
        return KIOXIA_BASE / "voice_refs"


# 参考音频根目录（按 agent 分子目录）
_VOICE_UPLOAD_DIR = _get_voice_upload_dir()
_AUDIO_EXTS = {".mp3", ".wav"}


def get_voice_upload_dir() -> Path:
    """返回参考音频根目录（不存在则创建）。"""
    _VOICE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _VOICE_UPLOAD_DIR


def get_agent_voice_dir(agent: str) -> Path:
    """返回指定 agent 的参考音频目录（不存在则创建）。"""
    d = _VOICE_UPLOAD_DIR / agent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_builtin_voices():
    """将内置参考音频复制到按 agent 分目录的结构中（仅首次）。"""
    import shutil
    import sys
    # 扫描源目录中所有音频文件，按文件名前缀分配到对应 agent 目录
    _src_dirs = []
    # 用户数据目录 — 旧格式直接放在 .ai-agent 根目录
    if KIOXIA_BASE.exists():
        _src_dirs.append(KIOXIA_BASE)
    # 用户数据目录 — 旧格式放在 data 根目录
    try:
        from config import VOICE_REF_DIR
        _data_base = VOICE_REF_DIR.parent
        if _data_base.exists() and _data_base != KIOXIA_BASE:
            _src_dirs.append(_data_base)
    except ImportError:
        pass
    # PyInstaller 打包内置
    if getattr(sys, "_MEIPASS", None):
        _src_dirs.append(Path(sys._MEIPASS) / "assets" / "voice_refs")
    # 开发环境
    _dev_dir = Path(__file__).resolve().parent.parent / "assets" / "voice_refs"
    if _dev_dir.exists():
        _src_dirs.append(_dev_dir)

    for src_dir in _src_dirs:
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        for f in sorted(src_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in _AUDIO_EXTS:
                continue
            # 按文件名前缀匹配 agent（xiaoda_hq.wav → xiaoda, xiaoli.mp3 → xiaoli）
            # 改名遗留兼容：nahida 前缀也映射到 xiaoda（纳西妲→小妲改名前的旧文件名）
            stem = f.stem.lower()
            matched_agent = None
            _prefix_to_agent = {
                "xiaoda": "xiaoda",
                "nahida": "xiaoda",  # 兼容旧文件名 nahida.wav / nahida_hq.wav
                "xiaoli": "xiaoli",
                "xiaoke": "xiaoke",
                "xiaolian": "xiaolian",
                "xiaolang": "xiaolang",
            }
            for prefix, agent in _prefix_to_agent.items():
                if stem.startswith(prefix):
                    matched_agent = agent
                    break
            if not matched_agent:
                continue
            dest_dir = _VOICE_UPLOAD_DIR / matched_agent
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f.name
            if not dest.exists():
                shutil.copy2(str(f), str(dest))


def list_all_voices() -> dict:
    """返回按 agent 分组的参考音频，{agent: [{name, path}]}。"""
    _ensure_builtin_voices()
    result = {}
    if _VOICE_UPLOAD_DIR.exists():
        for agent_dir in sorted(_VOICE_UPLOAD_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            voices = []
            for f in sorted(agent_dir.iterdir()):
                if f.suffix.lower() in _AUDIO_EXTS:
                    voices.append({"name": f.stem, "path": f})
            if voices:
                result[agent_dir.name] = voices
    return result


def resolve_voice_path(voice: str) -> Path | None:
    """解析音色到音频文件路径。支持 'agent/name' 格式和旧格式兼容。"""
    # 新格式: agent/name
    if "/" in voice:
        agent, name = voice.split("/", 1)
        agent_dir = _VOICE_UPLOAD_DIR / agent
        if agent_dir.exists():
            for f in agent_dir.iterdir():
                if f.stem == name and f.suffix.lower() in _AUDIO_EXTS:
                    return f
    # 旧格式兼容: 直接是 VOICE_REFERENCES 的 key
    if voice in VOICE_REFERENCES:
        return VOICE_REFERENCES[voice]
    # 旧格式兼容: 在所有 agent 目录中查找 stem
    if _VOICE_UPLOAD_DIR.exists():
        for agent_dir in _VOICE_UPLOAD_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            for f in agent_dir.iterdir():
                if f.stem == voice and f.suffix.lower() in _AUDIO_EXTS:
                    return f
    return None

VOICE_STYLES = {
    "xiaoda": (
        "角色：{agent_name}，须弥的草神，须弥的守护者。外表是可爱的小女孩，实际已承载五百年孤独与智慧。"
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
    "xiaoli": (
        "角色：{agent_name}，火花骑士，蒙德城最可爱的小小爆破专家。活泼开朗的小女孩，充满童真和冒险精神。\n\n"
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


def _get_voice_style(style_key: str) -> str:
    """返回注入了 agent display_name 的语音风格描述；未知 key 返回空串。"""
    template = VOICE_STYLES.get(style_key, "")
    if not template:
        return ""
    return template.format(agent_name=get_agent_display_name(style_key))

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
    "coquettish": "(撒娇地轻声说，声音甜糯上扬，语速时快时慢，带着哼哼的尾音)",
}

_cache: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX_SIZE = 50


async def _encode_voice_file(path: Path) -> str:
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

    data = await asyncio.to_thread(path.read_bytes)
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
    """驱动文本到语音合成的引擎。"""
    _SYNTHESIS_CACHE_MAX_SIZE = 200

    _PRECOMPOSED_PHRASES: ClassVar[dict[str, list[tuple[str, str]]]] = {
        "xiaoda": [
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

    def __init__(self) -> None:
        """初始化 TTS 引擎 (未初始化客户端, 需调用 init)."""
        self._client: AsyncOpenAI | None = None
        self._output_dir: Path | None = None
        self._available = False
        self._synthesis_cache: OrderedDict[str, Path] = OrderedDict()
        self._cache_index_path: Path | None = None

    def _load_cache_index(self) -> None:
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
            logger.warning("tts.cache_index_load_failed error={}", str(e))

    def _save_cache_index(self) -> None:
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
            logger.warning("tts.cache_index_save_failed error={}", str(e))

    async def init(self, output_dir: str | Path | None = None) -> None:
        """初始化 TTS 客户端与缓存目录.

        Args:
            output_dir: 合成音频输出目录, None 表示使用默认目录
        """
        api_key = _get_mimo_api_key()
        if not api_key:
            logger.warning("tts.no_api_key")
            return

        self._client = AsyncOpenAI(api_key=api_key, base_url=MIMO_BASE_URL)

        if output_dir:
            self._output_dir = Path(output_dir)
        else:
            try:
                from config import DATA_DIR
                self._output_dir = DATA_DIR / "tts_cache"
            except ImportError:
                self._output_dir = KIOXIA_BASE / "xiaoda-data" / "tts_cache"
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 设置缓存索引路径并加载已有缓存
        self._cache_index_path = self._output_dir / "cache_index.json"
        self._load_cache_index()

        any_voice_available = False
        for name, path in VOICE_REFERENCES.items():
            if path.exists():
                try:
                    await _encode_voice_file(path)
                    any_voice_available = True
                    logger.info("tts.voice_ready", voice=name)
                except Exception as e:
                    logger.warning("tts.voice_load_failed voice={} error={}", name, str(e))
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
        """返回 TTS 是否可用 (客户端就绪且已通过健康检查)."""
        return self._available and self._client is not None

    def refresh_client(self) -> None:
        """重建 TTS 客户端（Setup 保存新 Key 后调用）。"""
        old_client = self._client
        api_key = _get_mimo_api_key()
        if api_key:
            self._client = AsyncOpenAI(api_key=api_key, base_url=MIMO_BASE_URL)
            self._available = True
            logger.info("tts.client_refreshed key_hash={}",
                        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8] if api_key else "***")
        else:
            self._client = None
            self._available = False
        # 关闭旧客户端释放连接
        if old_client is not None and old_client is not self._client:
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                _bg_close = loop.create_task(old_client.close())
            except RuntimeError:
                pass

    async def synthesize(
        self,
        text: str,
        voice: str = "xiaoda",
        style: str = "",
        emotion: str = "",
        output_path: str | Path | None = None,
    ) -> Path | None:
        """合成语音文件, 命中缓存则直接返回路径.

        Args:
            text: 待合成文本
            voice: 音色名, 默认 xiaoda
            style: 风格描述, 默认空字符串
            emotion: 情绪描述, 默认空字符串
            output_path: 输出路径, None 表示自动生成

        Returns:
            合成音频文件路径, 失败返回 None
        """
        # 防御性清理: 移除可能残留的情绪/表情包标签, 防止TTS朗读标签文本 (BUG-18)
        text = re.sub(r'\[emotion:[^\]]*\]', '', text)
        text = re.sub(r'\[sticker:[^\]]*\]', '', text)
        text = text.strip()

        if not self.available:
            logger.warning("tts.not_available")
            return None

        if voice not in VOICE_REFERENCES:
            # 尝试动态解析用户上传的参考音频
            voice_path = resolve_voice_path(voice)
            if voice_path is None:
                logger.warning("tts.unknown_voice", voice=voice)
                return None
        else:
            voice_path = VOICE_REFERENCES[voice]
        if not voice_path.exists():
            logger.error("tts.voice_file_missing_unavailable", voice=voice, message="语音参考文件缺失，无法合成。请检查音频文件是否存在。")
            return None

        # 计算缓存 key 并检查命中
        cache_key = hashlib.md5(f"{voice}:{emotion}:{text}".encode(), usedforsecurity=False).hexdigest()
        cached = self._tts_cache_hit(cache_key)
        if cached is not None:
            return cached
        logger.info("tts.cache_miss", key=cache_key)

        try:
            voice_data_url = await _encode_voice_file(voice_path)
        except Exception as e:
            logger.error("tts.voice_encode_failed error={}", str(e))
            return None

        messages = self._build_tts_messages(voice, text, style, emotion)

        try:
            completion = await self._call_tts_with_retry(voice, voice_data_url, messages)
            if completion is None:
                return None

            message = completion.choices[0].message
            if message.audio is None or not getattr(message.audio, "data", None):
                logger.warning("tts.no_audio_returned")
                return None

            audio_bytes = base64.b64decode(message.audio.data)
            if len(audio_bytes) < 1024:
                logger.warning("tts.audio_too_small", voice=voice,
                               size=len(audio_bytes), text_len=len(text))
                return None

            return self._save_tts_output(audio_bytes, voice, text, output_path, cache_key)
        except Exception as e:
            logger.error("tts.synthesize_failed voice={} error={}", voice, str(e))
            return None

    def _tts_cache_hit(self, cache_key: str) -> Path | None:
        """检查 TTS 合成缓存。命中返回 Path，未命中返回 None。"""
        if cache_key not in self._synthesis_cache:
            return None
        cached_path = self._synthesis_cache[cache_key]
        if cached_path.exists():
            self._synthesis_cache.move_to_end(cache_key)
            logger.info("tts.cache_hit", key=cache_key, file=str(cached_path))
            return cached_path
        del self._synthesis_cache[cache_key]
        return None

    def _build_tts_messages(self, voice: str, text: str, style: str, emotion: str) -> list[dict]:
        """构造 MiMo 导演模式消息：user 放角色/场景/指导，assistant 放音频标签+文本。"""
        # voice 可能是 "agent/name" 格式，取 agent 部分查 VOICE_STYLES
        style_key = voice.split("/", 1)[0] if "/" in voice else voice
        context = style or _get_voice_style(style_key)
        style_tag = EMOTION_STYLE_MAP.get(emotion, EMOTION_STYLE_MAP["neutral"])

        # 统一模式：通过 resolve_emotion 解析后再查 TTS_STYLE_MAP
        if is_unified() and emotion:
            resolved = resolve_emotion(emotion)
            tts_style = TTS_STYLE_MAP.get(resolved, "neutral")
            style_tag = EMOTION_STYLE_MAP.get(tts_style, EMOTION_STYLE_MAP["neutral"])

        messages = []
        if context:
            messages.append({"role": "user", "content": context})
        else:
            voice_style = _get_voice_style(style_key)
            if voice_style:
                messages.append({"role": "user", "content": voice_style})
        # 音频标签放在 assistant 消息开头，紧接要合成的文本
        messages.append({"role": "assistant", "content": f"{style_tag} {text}"})
        return messages

    async def _call_tts_with_retry(self, voice: str, voice_data_url: str,
                                   messages: list[dict]) -> Any:
        """调用 TTS API，429 限流时退避重试。返回 completion 或 None。"""
        for _attempt in range(3):
            try:
                return await self._client.chat.completions.create(
                    model=MIMO_TTS_MODEL,
                    messages=messages,
                    audio={
                        "format": "mp3",
                    "voice": voice_data_url,
                    },
                )
            except Exception as api_err:
                if "429" in str(api_err) and _attempt < 2:
                    wait = (_attempt + 1) * 5
                    logger.warning("tts.rate_limited_retry", voice=voice, attempt=_attempt + 1, wait=wait)
                    await asyncio.sleep(wait)
                else:
                    raise
        return None

    def _save_tts_output(self, audio_bytes: bytes, voice: str, text: str,
                         output_path: str | Path | None, cache_key: str) -> Path:
        """写入音频文件并更新缓存，返回输出 Path。"""
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

    async def synthesize_xiaoda(self, text: str, style: str = "", emotion: str = "") -> Path | None:
        """使用 xiaoda 音色合成语音 (synthesize 的便捷封装)."""
        return await self.synthesize(text, voice="xiaoda", style=style, emotion=emotion)

    async def synthesize_xiaoli(self, text: str, style: str = "", emotion: str = "") -> Path | None:
        """使用 xiaoli 音色合成语音 (synthesize 的便捷封装)."""
        return await self.synthesize(text, voice="xiaoli", style=style, emotion=emotion)

    async def precompose_phrases(self) -> None:
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

    async def close(self) -> None:
        """关闭 TTS 引擎，释放资源"""
        if hasattr(self, '_client') and self._client is not None:
            await self._client.close()
            self._client = None
