import os
import base64
import time
from pathlib import Path
from openai import AsyncOpenAI
from loguru import logger

MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_TTS_MODEL = "mimo-v2.5-tts-voiceclone"

KIOXIA_BASE = Path(os.getenv("KIOXIA_DATA_DIR", "/media/orangepi/KIOXIA/nahida-data")).parent

VOICE_REFERENCES = {
    "nahida": KIOXIA_BASE / "nahida.wav",
    "keli": KIOXIA_BASE / "keli.mp3",
}

VOICE_STYLES = {
    "nahida": "温柔可爱的少女音，语调轻柔甜美，偶尔带点俏皮，像草神纳西妲在说话",
    "keli": "活泼开朗的少女音，语调上扬欢快，充满活力，像火花骑士可莉在说话",
}

_cache: dict[str, str] = {}


def _encode_voice_file(path: Path) -> str:
    key = str(path)
    if key in _cache:
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
    _cache[key] = data_url
    logger.info("tts.voice_cached", file=str(path), size_kb=len(data) // 1024)
    return data_url


class TTSEngine:
    def __init__(self):
        self._client: AsyncOpenAI | None = None
        self._output_dir: Path | None = None
        self._available = False

    async def init(self, output_dir: str | Path | None = None):
        if not MIMO_API_KEY:
            logger.warning("tts.no_api_key")
            return

        self._client = AsyncOpenAI(api_key=MIMO_API_KEY, base_url=MIMO_BASE_URL)

        if output_dir:
            self._output_dir = Path(output_dir)
        else:
            self._output_dir = KIOXIA_BASE / "nahida-data" / "tts_cache"
        self._output_dir.mkdir(parents=True, exist_ok=True)

        for name, path in VOICE_REFERENCES.items():
            if path.exists():
                try:
                    _encode_voice_file(path)
                    logger.info("tts.voice_ready", voice=name)
                except Exception as e:
                    logger.warning("tts.voice_load_failed", voice=name, error=str(e))

        self._available = True
        logger.info("tts.engine_initialized")

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    async def synthesize(
        self,
        text: str,
        voice: str = "nahida",
        style: str = "",
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
            logger.warning("tts.voice_file_missing", voice=voice)
            return None

        try:
            voice_data_url = _encode_voice_file(voice_path)
        except Exception as e:
            logger.error("tts.voice_encode_failed", error=str(e))
            return None

        context = style or VOICE_STYLES.get(voice, "")

        messages = []
        if context:
            messages.append({"role": "user", "content": context})
        messages.append({"role": "assistant", "content": text})

        try:
            completion = await self._client.chat.completions.create(
                model=MIMO_TTS_MODEL,
                messages=messages,
                audio={
                    "format": "mp3",
                    "voice": voice_data_url,
                },
            )

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

            logger.info("tts.synthesized", voice=voice, text_len=len(text), file=str(out), size_kb=len(audio_bytes) // 1024)
            return out

        except Exception as e:
            logger.error("tts.synthesize_failed", voice=voice, error=str(e))
            return None

    async def synthesize_nahida(self, text: str, style: str = "") -> Path | None:
        return await self.synthesize(text, voice="nahida", style=style)

    async def synthesize_keli(self, text: str, style: str = "") -> Path | None:
        return await self.synthesize(text, voice="keli", style=style)
