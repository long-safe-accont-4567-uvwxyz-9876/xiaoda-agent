"""VoiceMixin —— Phase 1 拆分自 message_processor.py。

包含语音意图检测（_detect_voice_intent）、语音强制开关（_resolve_voice_force）、
语音结果构建（_build_voice_result）及 TTS 触发决策模块级工具函数
（_get_temperature / _is_suitable_for_voice / _should_auto_tts / _decide_tts_trigger）。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared 及 config/core 叶子模块，
不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from agent_core._shared import _pending_tts_audio
from config import TTS_ASYNC_MODE
from core.degradation_strategy import get_degradation_strategy


def _get_temperature(model_cfg: dict | None = None) -> float:
    """读取 temperature：优先 webui_overrides，回退 agent.json5 默认值。"""
    from config import get_temperature
    default = float(model_cfg.get("temperature", 0.7)) if model_cfg else 0.7
    return get_temperature(default=default)


# ── TTS 触发时机控制（v2：移除冷却，改为智能时机判断） ─────────
# 设计背景：用户反馈 voice_mode 开启后 TTS "失控"——不分场合地每条都触发语音，
#   连代码块/URL/子 agent 技术回复也朗读。冷却（8s/30s）治标不治本：
#   只降频率不解决"该不该发"，且只对主路径生效，子 agent 路径完全没冷却。
# 新方案：4 个触发点统一过 _decide_tts_trigger，用内容适宜性守卫替代冷却。
#   - force_voice（用户显式"发语音"）→ 信任用户意图，直接通过
#   - voice_mode（粘性语音模式）→ 必须通过 _is_suitable_for_voice 内容守卫
#   - 适合语音：自然人语（闲聊/问候/情感/解释）
#   - 不适合语音：代码块/URL/文件路径/标签/DSML 残留/极短/超长


# 语音适宜性守卫的特征常量：命中即视为技术内容（不适合语音朗读）
_CODE_KEYWORD_SIGS = ('def ', 'class ', 'import ', 'from ',
                      'function ', 'const ', 'return ')
_PATH_SIGS = ('/home/', '/usr/', '/var/', '/etc/', '/tmp/',
              '.py', '.js', '.json', '.md', '.txt', '.sh')
_TOOL_TAG_SIGS = ('<tool_result', '<tool_call', '[sticker:', '[emotion:')


def _looks_like_code_or_url(cleaned: str) -> bool:
    """判断是否含代码块/代码关键字/JSON/URL 等技术内容。"""
    if '```' in cleaned:
        return True
    if any(sig in cleaned for sig in _CODE_KEYWORD_SIGS):
        return True
    # 大括号出现 ≥2 次（JSON/代码块特征）
    if cleaned.count('{') >= 2 or cleaned.count('}') >= 2:
        return True
    # 单层 JSON 对象特征（如 {"key": "value"}，仅 1 对大括号但明显是结构化数据）
    if '{"' in cleaned or '": "' in cleaned:
        return True
    # 任何 URL 都不朗读（原阈值 url_count>=2 太宽松，含 1 个 URL 即视为技术内容）
    if 'http://' in cleaned or 'https://' in cleaned:
        return True
    return False


def _looks_like_path_or_tag(cleaned: str) -> bool:
    """判断是否含文件路径/标签/DSML 残留等技术内容。"""
    if any(p in cleaned for p in _PATH_SIGS):
        return True
    # 纯标签内容（如 [emotion:xxx] [sticker:xxx]）
    if cleaned.startswith('[') and cleaned.endswith(']') and ':' in cleaned:
        return True
    if any(tag in cleaned for tag in _TOOL_TAG_SIGS):
        return True
    return False


def _is_suitable_for_voice(reply: str) -> bool:
    """判断回复内容是否适合语音朗读（替代原 _should_auto_tts 守卫）。

    设计原则：TTS 适合"自然人语"，不适合"技术内容"。
    voice_mode 开启后每条都过此守卫，避免代码/URL/路径被朗读导致"失控"。
    force_voice（用户显式要求发语音）不走此守卫，信任用户选择。
    """
    if not reply:
        return False
    cleaned = reply.strip()
    if not cleaned or len(cleaned) < 8:
        return False  # 极短回复不朗读（原阈值 5 太低）
    if len(cleaned) > 400:
        return False  # 超长回复（>400字）不朗读，语音太长体验差
    if _looks_like_code_or_url(cleaned):
        return False
    if _looks_like_path_or_tag(cleaned):
        return False
    # 纯数字/纯符号（字母与中文字符占比 < 40% 视为非自然语言）
    letters = [c for c in cleaned if c.isalpha() or '\u4e00' <= c <= '\u9fff']
    if len(letters) < len(cleaned) * 0.4:
        return False
    return True


def _should_auto_tts(reply: str) -> bool:
    """[向后兼容别名] 检查回复内容是否适合自动生成 TTS。

    保留函数名防止外部引用断裂；内部委托给 _is_suitable_for_voice。
    """
    return _is_suitable_for_voice(reply)


def _decide_tts_trigger(reply: str, *, force_voice: bool, voice_mode: bool,
                        tts_available: bool, tts_enabled: bool) -> bool:
    """统一 TTS 触发决策：返回 True 才生成语音。

    4 个触发点（greeting 短路 / 主路径 _build_voice_result / 子 agent 串行 / 子 agent 并行）
    都过此函数，确保守卫逻辑一致，避免子 agent 路径漏守卫导致"失控"。

    时机判断（替代原冷却机制）：
      - 既非 force_voice 也非 voice_mode → 不触发
      - TTS 不可用 / 功能降级关闭 / 回复为空或过短 → 不触发
      - force_voice（用户显式"发语音"）→ 信任用户意图，直接通过（不受守卫限制）
      - voice_mode（粘性语音模式）→ 必须通过 _is_suitable_for_voice 内容守卫
    """
    if not (force_voice or voice_mode):
        return False
    if not (tts_available and tts_enabled and reply and len(reply.strip()) > 2):
        return False
    if force_voice:
        return True  # 用户显式意图，信任选择
    return _is_suitable_for_voice(reply)

class VoiceMixin:
    """语音相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    def _resolve_voice_force(self, clean_input: str) -> bool:
        """根据语音意图切换 voice_mode，返回本轮是否强制语音。"""
        voice_intent = self._detect_voice_intent(clean_input)
        if voice_intent == "off":
            self.set_voice_mode(False)
            return False
        if voice_intent == "on":
            return not self._voice_mode
        return False

    async def _build_voice_result(self, clean_reply: Any, emotion_label: Any, force_voice: Any) -> tuple:
        """构建语音合成结果。返回 (audio_path, tts_pending, tts_text)。

        优先级：synthesize_voice 工具生成的音频 > force_voice（一次性）> _voice_mode（自动触发，有守卫）

        TTS 时机控制 v2：统一过 _decide_tts_trigger（移除冷却，改为内容适宜性守卫）
        """
        # 1. 优先检查 LLM 主动调用 synthesize_voice 工具生成的音频
        tool_audio = _pending_tts_audio.get()
        if tool_audio is not None:
            _pending_tts_audio.set(None)  # 消费后清除，避免泄漏到下一轮
            logger.info("tts.tool_audio_used", audio_path=str(tool_audio))
            return tool_audio, False, ""

        # 2. 统一触发决策（替代原 should_generate_voice + 内容守卫 + 冷却守卫三层判定）
        if not _decide_tts_trigger(
                clean_reply, force_voice=force_voice, voice_mode=self._voice_mode,
                tts_available=self.tts.available,
                tts_enabled=get_degradation_strategy().is_feature_available("tts")):
            return None, False, ""

        # 3. 生成（异步 pending 或同步合成）
        audio_path = None
        tts_pending = False
        tts_text = ""
        if TTS_ASYNC_MODE:
            tts_pending = True
            tts_text = self._clean_reply(clean_reply)
        else:
            try:
                audio_path = await self.tts.synthesize_xiaoda(
                    self._clean_reply(clean_reply), emotion=emotion_label)
            except Exception as e:
                logger.warning("agent.tts_failed", error=str(e))
        return audio_path, tts_pending, tts_text

    def _detect_voice_intent(self, user_input: str) -> str:
        """检测语音意图：三态返回 'none' / 'on' / 'off'。

        - 'off': 含关闭意图（如"不要发语音了"/"关闭语音"→关语音）
        - 'on':  含明确"用语音回复"动作意图（如"发语音"/"念给我听"→开语音）
        - 'none': 无明确语音动作意图

        收紧原则（v2）：必须有"动作意图"才触发 on，单纯提及"语音/声音/说话"不触发。
        回归案例：用户说"语音识别怎么实现"/"声音大点"/"说话方式怪怪的"被旧关键词
        "语音"/"声音"/"说话"误匹配 → voice_mode 被永久打开 → TTS 失控。
        旧案例 id=1993 "不要发语音了"：现 off 关键词优先匹配，避免被 on 的"发语音"误判。
        """
        # 强意图：明确的"用语音回复"动作（必须含动作语义：发/用/念/读/说给/开启/打开）
        on_keywords = [
            "发语音", "用语音", "语音回复", "语音消息", "语音说",
            "念给我", "念出来", "读给我听", "说给我听",
            "开启语音", "打开语音", "语音模式", "开启语音模式",
            "用声音回复", "用声音说", "语音是开的", "语音打开了",
            "用tts", "用voice",
        ]
        # 关闭意图：完整的 off 关键词（不再依赖"否定词前缀 4 字符"检测，更可靠）
        off_keywords = [
            "关闭语音", "语音关闭", "关闭语音模式", "语音是关的", "关掉语音",
            "不要语音", "不用语音", "别发语音", "停止语音",
            "不要发语音", "别用语音",
        ]
        q = user_input.lower()
        # off 优先检测（避免"不要发语音了"被 on 的"发语音"误匹配）
        for kw in off_keywords:
            if kw in q:
                return "off"
        for kw in on_keywords:
            if kw in q:
                return "on"
        return "none"

