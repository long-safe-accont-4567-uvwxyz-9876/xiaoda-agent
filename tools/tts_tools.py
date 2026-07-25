"""TTS 语音合成工具 — 让 LLM 能够主动调用语音合成。

设计目的：
- 将 TTS 从 sticky 自动触发（/voice on 后每条回复都生成）改为 LLM 可主动调用的工具
- LLM 根据对话语境判断何时适合发语音（如用户说"读给我听"、情感时刻等）
- 工具合成后通过 _pending_tts_audio ContextVar 将音频路径回传到 ProcessResult.audio_path
- 保留 /voice on|off 作为系统提示词 hint（不再自动触发每条回复的 TTS）
"""

import re
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolResult, ToolPermission


@register_tool(
    name="synthesize_voice",
    description=(
        "将文字合成为语音消息并发送给用户。"
        "适用场景：用户明确要求听语音（如'读给我听''发语音'）、"
        "情感丰富的回复（如安慰、撒娇、讲故事）、用户开启了语音模式。"
        "不适用场景：纯代码/命令输出、极短回复（如'嗯''好的'）、"
        "包含大量 URL 或技术参数的内容。"
        "调用时传入要朗读的文本，可选传入情绪以调整语音风格。"
    ),
    schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要合成为语音的文本内容（应为自然语言，不含代码/URL/标签）",
            },
            "emotion": {
                "type": "string",
                "description": "情绪风格（可选）：happy/excited/sad/angry/anxious/shy/surprised/"
                               "neutral/greeting/caring/playful/lonely/curious/thinking/coquettish",
            },
        },
        "required": ["text"],
    },
    permission=ToolPermission.READ_ONLY,
)
async def synthesize_voice(text: str, emotion: str = "") -> ToolResult:
    """合成语音并通过 _pending_tts_audio ContextVar 回传音频路径。"""
    if not text or not text.strip():
        return ToolResult.fail("文本内容为空，无法合成语音")

    # 防御性清理：移除可能残留的情绪/表情包标签（与 TTSEngine.synthesize 一致）
    text = re.sub(r'\[emotion:[^\]]*\]', '', text)
    text = re.sub(r'\[sticker:[^\]]*\]', '', text)
    text = text.strip()

    if len(text) < 2:
        return ToolResult.fail("文本内容过短，不适合合成语音")

    # 获取全局 TTS 引擎
    from emotion.tts_engine import get_tts_engine
    tts = get_tts_engine()
    if tts is None or not tts.available:
        return ToolResult.fail("TTS 语音引擎未初始化或不可用（可能未配置 API Key 或参考音频缺失）")

    # 降级模式检查
    try:
        from core.degradation_strategy import get_degradation_strategy
        if not get_degradation_strategy().is_feature_available("tts"):
            return ToolResult.fail("TTS 功能当前处于降级模式，暂时不可用")
    except Exception:
        pass  # 降级策略检查失败不阻塞，让 TTS 引擎自行判断

    try:
        audio_path = await tts.synthesize_xiaoda(text, emotion=emotion)
        if audio_path is None:
            return ToolResult.fail("语音合成失败（API 限流或返回空音频），请稍后重试")

        # 通过 ContextVar 将音频路径回传到 ProcessResult.audio_path
        from agent_core._shared import _pending_tts_audio
        _pending_tts_audio.set(audio_path)

        logger.info("tts.tool_synthesized", text_len=len(text), emotion=emotion,
                     audio_path=str(audio_path))
        return ToolResult.ok(f"语音已生成（{len(text)}字），将随回复一起发送给用户")
    except Exception as e:
        logger.error("tts.tool_synthesize_failed error={}", str(e))
        return ToolResult.fail(f"语音合成失败: {e}")
