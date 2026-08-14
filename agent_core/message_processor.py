"""消息处理 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含主处理流程 _process_impl 及消息分类、语音意图识别、图片描述、
聊天目标路由等消息处理相关方法。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import tempfile
from collections import OrderedDict
from datetime import datetime
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

import openai as _openai_mod  # P0 Task 1.8：用于捕获 BadRequestError/APIError


from utils.common import safe_int as _safe_int, DEFAULT_MAX_TOKENS

from loguru import logger

from config import (MIMO_MODEL, AGENT_CONFIG, build_safe_system_prompt,
                    TTS_ASYNC_MODE,
                    SIMPLE_CHAT_FASTPATH, STREAM_TEXT_PUSH, get_agent_display_name)
from prompt_builder import build_scene_aware_prompt
from core.chat_processor import ChatProcessor
from core.circuit_breaker import CircuitState
from core.background_tasks import _spawn
from core.degradation_strategy import get_degradation_strategy
from emotion.emotion_simple import detect_emotion, build_emotion_hint
from emotion.emotion_enum import CN_TO_EN, is_unified, ensure_emotion_tag
from tool_engine.tool_registry import to_openai_tools
from utils.text_utils import (has_dsml_tool_calls, parse_dsml_tool_calls,
                              humanize, encode_image_to_base64, strip_reasoning,
                              strip_dsml, ends_with_valid_ending, is_reply_likely_complete)
from utils.llm_cleanup import deduplicate_multi_reply, merge_continuation

# 从 _shared 导入共享常量, 避免重复定义 (该模块极轻量, 无循环导入风险)
from agent_core._shared import (
    DEGRADED_REPLY, is_degraded_reply,
    get_empty_reply_for_finish_reason,
    _pending_tts_audio,
    _current_request_ctx,
    _stream_finish_reason_var,
)


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
    # 代码块
    if '```' in cleaned:
        return False
    # 代码特征关键字（def/class/import/from/function/const/return）
    if any(sig in cleaned for sig in ('def ', 'class ', 'import ', 'from ',
                                       'function ', 'const ', 'return ')):
        return False
    # 大括号出现 ≥2 次（JSON/代码块特征）
    if cleaned.count('{') >= 2 or cleaned.count('}') >= 2:
        return False
    # 单层 JSON 对象特征（如 {"key": "value"}，仅 1 对大括号但明显是结构化数据）
    if '{"' in cleaned or '": "' in cleaned:
        return False
    # 任何 URL 都不朗读（原阈值 url_count>=2 太宽松，含 1 个 URL 即视为技术内容）
    if 'http://' in cleaned or 'https://' in cleaned:
        return False
    # 文件路径特征
    if any(p in cleaned for p in ('/home/', '/usr/', '/var/', '/etc/', '/tmp/',
                                   '.py', '.js', '.json', '.md', '.txt', '.sh')):
        return False
    # 纯标签内容（如 [emotion:xxx] [sticker:xxx]）
    if cleaned.startswith('[') and cleaned.endswith(']') and ':' in cleaned:
        return False
    # 工具结果 / DSML 残留
    if any(tag in cleaned for tag in ('<tool_result', '<tool_call',
                                       '[sticker:', '[emotion:')):
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

if TYPE_CHECKING:
    from agent_core._shared import RequestContext
from agent_core._shared import ProcessResult

# P0-2 修复：_system_context 改为 ContextVar，避免单例 AgentCore 并发覆写。
# 主动问候(nudge_engine)与用户消息并发时，实例属性 self._system_context 互相覆写，
# 导致场景提示串台。ContextVar 在 asyncio.Task 级别隔离，每个请求读到自己设置的值。
from contextvars import ContextVar as _ContextVar
_system_context_var: _ContextVar[str] = _ContextVar("_system_context", default="")


# ── G1: 问候短路（模块级编译正则，一次编译多次使用） ───────────
_GREETING_PATTERN = re.compile(
    r'^(你好|您好|hi|hello|hey|嗨|在吗|在不在|在么|'
    r'早安|早上好|早|午安|下午好|晚上好|晚安|'
    r'谢谢|感谢|thanks|thx|多谢)\s*[!！。.～~？?]*$',
    re.IGNORECASE
)

_THANK_REPLIES = ["不客气～", "不用谢啦～", "举手之劳～"]

# reunion_reflection: 用户"回来了"类关键词（用于生成个性化重聚欢迎）
_REUNION_PATTERN = re.compile(
    r'(回来了|我回来了|回来啦|我回来啦|回来了吗|到家了|上线了)',
    re.IGNORECASE,
)

# G1: 项目硬约束 —— 所有时间函数使用 Asia/Shanghai 时区
_SH_TZ = ZoneInfo("Asia/Shanghai")

# 时段问候：(start_hour, end_hour, reply) — end_hour 可超过 24 以覆盖跨夜时段
_TIME_GREETINGS = [
    (5, 12, "早上好～新的一天开始啦，今天也要加油哦！"),
    (12, 18, "下午好～今天过得怎么样呀？"),
    (18, 22, "晚上好～今天辛苦啦，有什么想聊的吗？"),
    (22, 30, "夜深啦～记得早点休息哦，有什么事明天再说？"),
]


def _is_greeting_enabled() -> bool:
    """读取 ENABLE_GREETING_SHORTCUT 开关（默认 false）。

    用户反馈：模板回复（"早上好～新的一天开始啦"）缺乏上下文感知，
    不如让 LLM 生成自然、有人格温度的问候。默认关闭，让"你好"走 LLM。
    如需恢复模板短路，设置 ENABLE_GREETING_SHORTCUT=true。
    """
    return os.environ.get("ENABLE_GREETING_SHORTCUT", "false").lower() in ("true", "1", "yes")


class MessageProcessorMixin:
    """消息处理相关方法的 Mixin，由 AgentCore 组合使用。"""

    # ── Harness 验收循环常量 ──────────────────────────────────
    # 重试机制保留：用于兜底异常截断（max_tokens 截断后续写、工具调用后回复不完整补全）。
    # 用户明确要求保留重试机制，不得缩减。
    MAX_VERIFICATION_TURNS = 8          # 最大循环轮次（保留原值，用于兜底截断恢复）
    # 放宽（2026-08-05 用户要求放宽微信/QQ 超时阈值）：
    # 根因：agnes 用户消息实测 11s（48 工具 prompt 长），原 10s 墙钟超时导致
    #   verification loop 强制停止 → 用户收不到完整回复。10→25 覆盖 agnes 11s +
    #   记忆 3s + 续写/工具 11s 余量。agnes 偶发慢也不超时。
    VERIFICATION_WALL_TIMEOUT = 25      # 墙钟超时（秒）
    MAX_CONSECUTIVE_TOOL_FAILURES = 3   # 连续工具失败上限
    # 放宽（2026-08-05 用户确认"对齐到30s消除误报"）：20→30。
    # 根因（超时铁证）：LLM_CALL_TIMEOUT=20 比 agnes transport 的 read=30s 更严格，
    #   agnes-2.0-flash 偶发慢（13s+，甚至 20s+）时，20s 一级超时先触发 → agent.model_error
    #   → 用户收到超时报错。agnes transport 本身 read 超时是 30s（AGNES_HTTP_TIMEOUT）。
    # 修复：LLM_CALL_TIMEOUT 对齐到 30s，与 agnes read 超时一致，agnes 偶发慢不再被一级
    #   超时误杀，能正常返回（可能慢但不报错）。这是消除"一直超时"的治本修复。
    LLM_CALL_TIMEOUT = 30               # 单次 LLM 调用超时

    # ── 跨对话回复去重（模型层面去重机制） ──────────────────
    # 根因：agnes-2.0-flash 在相似上下文（如角色扮演）下会生成高度相似的回复，
    #   如"像被电流贯穿一样瞬间僵住！手指死死抓着床单"在 5 次对话中出现 4 次。
    #   上下文虽含 assistant 历史回复，但模型未有效利用来避免重复。
    # 修复：维护每个 session 最近 N 条回复，新回复与之比较相似度，
    #   超阈值则追加"请用完全不同的表达方式"重试一次。
    REPLY_DEDUP_MAX = 5                 # 每 session 保留最近 5 条回复用于去重
    REPLY_DEDUP_THRESHOLD = 70.0        # rapidfuzz 0-100 刻度，>=70 视为重复
    REPLY_DEDUP_RETRY_TIMEOUT = 30      # 去重重试超时（秒），对齐 agnes read=30s
    # 治本修复（2026-08-05 用户"治标不治本"反馈）：6→10。
    # 根因：agnes-2.0-flash 正常响应 7s，但去重重试 timeout=6s < 7s →
    #   重试的 agnes 调用永远 6s 超时 → 用原回复（重复的）→ 去重机制完全失效！
    #   日志 reply.dedup_retry_timeout + 微信重复发送铁证。
    #   用户要求"重试后重复率必须低于70%"，但 6s 超时让重试永远不成功。
    # 10s 覆盖 agnes 7s + 3s 余量（截断历史后 agnes 更快，5-6s）。
    # 代价：重复回复时首次7s+重试10s=17s，但正常对话（不重复）7s达标。
    # 去重是用户要求的功能，不能牺牲。
    # P0 修复（2026-08-05 用户要求"10秒内响应"）：30→6。
    # 原值 30s 让去重总耗时 LLM(7s)+去重(30s)=37s，远超 10s 目标。
    # 6s 覆盖 agnes 生成简短去重回复（截断历史，比完整生成快）。
    # 权衡：正常不重复时 LLM(7s)+记忆(2s)=9s 达标；重复时触发去重 15s（略超 10s
    # 但远好于原 67s）。去重是异常路径，优先保证正常对话 10s 内响应。
    # P0 修复（内存泄漏根因）：session_id 含日期(SES-YYYYMMDD-...)，每天新增 key。
    # 原实现 _recent_replies: dict 的 session key 永不清理，长期运行无限增长 →
    # 内存泄漏 → 渐进退化。改为 OrderedDict + LRU，超过 cap 淘汰最久未访问的 session。
    #
    # cap 取值评估（确保不导致去重能力受限）：
    #   - c2c 场景：通常 1-5 个活跃用户，每用户 1 session
    #   - 群聊场景：session_id=qq_group:{openid}:...，每群活跃用户一个 session
    #     多群 + 大群可能 100+ 活跃 session
    #   - 取 256 覆盖群聊大群场景，正常使用永不触发淘汰 → 去重能力不受限
    #   - 内存上限：256 session × 5 reply × ~200字符 ≈ 256KB，完全可控
    #   - 极端场景（>256 活跃 session）LRU 淘汰最久未访问者，该用户下次对话
    #     去重历史为空重新积累，是 graceful 行为而非功能损坏
    REPLY_DEDUP_SESSION_CAP = 256       # 最大缓存 session 数，LRU 淘汰
    _recent_replies: "OrderedDict[str, list[str]]" = OrderedDict()  # session_id -> [reply1, ...]

    # ── 非主人工具白名单（信息查询 + 基础交互） ─────────────────
    ALLOWED_NON_MASTER_TOOLS: frozenset[str] = frozenset({
        # 搜索 / 信息
        "web_search", "get_weather", "search_cn", "wolfram_query",
        # 基础交互
        "get_current_time", "calculator", "nudge_greeting",
        "call_xiaoda",
    })

    async def _run_verification_loop(
        self,
        first_result: Any,
        messages: list[dict],
        tools: list[dict] | None,
        trace: Any,
        *,
        task_type: str,
        temperature: float,
        max_tokens: int | None,
        user_openid: str,
        session_id: str,
        is_owner: bool,
        ctx: RequestContext,
        user_input: str,
    ) -> tuple[str, list]:
        """Harness 验收循环：工具执行 → 结果回填 → 模型验收 → 循环。

        核心思想：工具调用后不直接 summarize，而是将结果追加到 messages，
        再次调用 LLM 让模型「验收」工具结果并生成最终回复。
        最多循环 MAX_VERIFICATION_TURNS 轮，墙钟超时 VERIFICATION_WALL_TIMEOUT 秒。
        """
        loop_start = time.time()
        consecutive_failures = 0
        all_tool_results: list = []

        # P0 治本修复：搜索类工具调用次数硬约束，防止 verification loop 不收敛。
        # 根因：每轮都传 tools+tool_choice=auto，LLM 拿到搜索结果后仍可继续搜索，
        # 实测 6 轮全在调 web_search/search_cn 不生成回复 → summarize 超时 → 原始结果倒出。
        # 修复：累计搜索类工具调用 ≥2 次后，后续轮次传 tools=None 强制 LLM 基于已有结果回答。
        _SEARCH_TOOLS = frozenset({"web_search", "search_cn", "multi_search",
                                    "web_browse", "web_browse_enhanced"})
        search_tool_call_count = 0
        _MAX_SEARCH_CALLS = 2

        # 解析首轮 LLM 输出（提取 tool_calls、assistant_content、reasoning）
        current_tool_calls, current_assistant_content, current_reasoning = \
            self._parse_verification_result(first_result, tools)

        # 如果首轮没有 tool_calls，检测回复完整性后返回
        if not current_tool_calls:
            if isinstance(first_result, str):
                reply = self._clean_reply(first_result)
                # 流式路径：从 ContextVar 读取最后一次流式调用的 finish_reason
                # 用于检测 max_tokens 截断（finish_reason="length"）
                # CodeRabbit 复审修复 #6：改为 ContextVar 读取，避免并发流式调用互相覆盖
                _finish_reason = _stream_finish_reason_var.get()
            else:
                _raw_content = first_result.choices[0].message.content or ""
                reply = self._clean_reply(_raw_content)
                _finish_reason = getattr(first_result.choices[0], "finish_reason", None)
                # 诊断：捕获清洗前原始内容，定位 empty_reply 根因
                if not reply or not reply.strip():
                    logger.warning("debug.empty_reply_raw_capture",
                                   raw_len=len(_raw_content),
                                   raw_head=_raw_content[:300],
                                   finish_reason=_finish_reason,
                                   has_tool_calls=bool(getattr(first_result.choices[0].message, "tool_calls", None)))

            # 空回复保护：根据 finish_reason 分类处理
            if not reply or not reply.strip():
                # P0 重构（用户明确要求"不许截断"）：
                # 移除 length 截断的"请继续"重试逻辑——该 prompt 会污染上下文，
                # LLM 在后续轮次回应"继续完成"等元词汇，造成角色出戏。
                # max_tokens 已提升到 131072，正常情况不会触发 length 截断。
                # 若仍触发（极端情况），直接降级返回提示，由上层 fallback 接管。
                if _finish_reason == "length":
                    trace.warning("verification.empty_first_reply_length_no_retry",
                                  hint="max_tokens=131072 下仍触发 length 截断，直接降级")
                    return get_empty_reply_for_finish_reason("length"), []
                elif _finish_reason == "content_filter":
                    # content_filter：内容被安全过滤，直接返回专用提示
                    trace.warning("verification.empty_first_reply_content_filter")
                    return get_empty_reply_for_finish_reason("content_filter"), []
                elif _finish_reason == "tool_calls":
                    # tool_calls：LLM 想调用工具但没生成文本，给个友好提示
                    trace.warning("verification.empty_first_reply_tool_calls_only")
                    return get_empty_reply_for_finish_reason("tool_calls"), []
                else:
                    # 未知 finish_reason（包括 None/stop 等），保留原 DEGRADED_REPLY 行为
                    trace.warning("verification.empty_first_reply",
                                  finish_reason=_finish_reason)
                    raise RuntimeError(f"empty_reply: LLM 返回空内容（finish_reason={_finish_reason}），触发 fallback")

            # 截断兜底：清洗英文泄漏 + length 截断重试（用户要求保留重试机制）
            # P0 修复（用户明确要求"我需要重试机制，但是不要给我提前截断了"）：
            # 原实现移除了所有重试，导致 finish_reason="length" 时直接 force_close，
            # 回复被截断 mid-sentence + 追加"。"（用户反复反馈"截断问题又出现了"根因）。
            # 修复策略（双层）：
            #   1. finish_reason="length" 时：用 assistant-prefill 续写重试（不追加 user message）
            #      ——这是用户要求的"兜底异常截断"重试机制
            #   2. 仍不完整时：用句末标点强制闭合（最后兜底）
            #   3. 英文推理泄漏：纯文本清洗，不触发 LLM 调用
            from utils.llm_cleanup import has_english_reasoning_leak as _has_eng_leak_fn
            from utils.llm_cleanup import strip_english_reasoning_leak as _strip_eng_leak_fn
            _reply_considered_complete = False
            _reply_rstripped = reply.rstrip()
            _ends_with_valid = ends_with_valid_ending(_reply_rstripped)
            _eng_leak = _has_eng_leak_fn(reply)
            if _eng_leak:
                # 英文推理泄漏 → 清洗（纯文本处理，不触发 LLM 调用）
                reply = _strip_eng_leak_fn(reply, context="verification_loop")
                _reply_rstripped = reply.rstrip()
                _ends_with_valid = ends_with_valid_ending(_reply_rstripped)
                if not reply.strip():
                    reply = DEGRADED_REPLY
                    logger.warning("verification.empty_after_leak_strip_degraded")
                    _reply_considered_complete = True
            # 完整性判定（P0 修复：emoji 感知 + 信任 LLM finish_reason="stop"）
            # 根因：原检查只认 "。！？～…）」】.!?」，角色扮演回复常以 emoji 结尾
            #       （如 "💕"、"😳💗"），被误判不完整 → 追加 "。" → 产生 "💕。"
            #       丑陋输出 + false-positive 截断警告（用户反复反馈"截断问题又出现了"）
            # 修复：
            #   1. 使用 ends_with_valid_ending() 包含 emoji 判定
            #   2. finish_reason="stop" + 长回复(>=30字符) → 信任 LLM，视为完整
            #      （模型风格不以标点结尾是正常的，不应强制追加 "。"）
            # P1-4 修复：内部场景（system_context 非空，如主动问候）跳过提前完成判定。
            # 根因：is_reply_likely_complete 对短问候判定"完整"直接接受，或判定"不完整"
            # 触发 force_close 追加"。"，两种情况都影响主动问候的自然度。
            # 修复：内部场景直接信任 LLM 输出，不做完整性判定/force_close。
            if _system_context_var.get():
                _reply_considered_complete = True
            elif not _eng_leak and is_reply_likely_complete(reply, _finish_reason):
                _reply_considered_complete = True
            elif _finish_reason == "length" and reply.strip() and len(reply) > 10:
                # P0 修复：finish_reason="length" 时用 assistant-prefill 续写重试
                # 用户明确要求保留重试机制用于兜底异常截断
                # 关键：不追加 "请继续完成" 等 user message（会污染上下文），
                #       只追加 assistant 消息让 LLM 从截断处续写
                try:
                    _retry_messages = list(messages)
                    _retry_messages.append({"role": "assistant", "content": reply})
                    _retry_result = await asyncio.wait_for(
                        self.router.route(
                            task_type, _retry_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            user_openid=user_openid, session_id=session_id,
                        ),
                        timeout=15,
                    )
                    _retry_text = ""
                    if isinstance(_retry_result, str):
                        _retry_text = _retry_result
                    else:
                        _retry_text = getattr(_retry_result.choices[0].message, "content", "") or ""
                    _retry_text = self._clean_reply(_retry_text)
                    if _retry_text and len(_retry_text) > 5:
                        # 合并续写内容
                        # P0 修复：merge_continuation 在 utils.llm_cleanup（非 utils.text_utils）
                        # 原错误导入导致 length_retry 每次都 ImportError → 重试失败 → 截断
                        # 已在模块顶部 import，无需局部导入
                        _merged, _action = merge_continuation(
                            reply, _retry_text, context="verification_length_retry",
                            assume_tail=True)
                        if _action != "discarded":
                            reply = _merged
                            logger.info("verification.length_retry_success",
                                        original_len=len(_reply_rstripped),
                                        final_len=len(reply), action=_action)
                            _reply_considered_complete = True
                        else:
                            logger.warning("verification.length_retry_duplicate",
                                           retry_len=len(_retry_text))
                    else:
                        logger.warning("verification.length_retry_empty",
                                       finish_reason=_finish_reason)
                except Exception as e:
                    logger.warning("verification.length_retry_failed",
                                   error=str(e)[:200], finish_reason=_finish_reason)
            elif _finish_reason is None and reply.strip() and len(reply) > 10 and not ends_with_valid_ending(reply.rstrip()):
                # P0 修复（qq_group 截断根因）：流式响应未收到 finish_reason
                # 根因：provider 中途关闭连接不发送 finish_reason chunk，
                #       _stream_finish_reason 保持 None，content 被静默截断。
                #       原 _finish_reason is None 时不触发任何重试，直接 force_close。
                # 修复：当 finish_reason is None 且 content 不以合法标记结尾时，
                #       视为潜在截断，用 assistant-prefill 续写重试（与 length 相同策略）。
                # 注意：仅在 content 不以 emoji/标点结尾时触发，避免对正常回复的误判。
                logger.warning("verification.stream_no_finish_retry",
                               reply_len=len(reply), finish_reason=_finish_reason)
                try:
                    _retry_messages = list(messages)
                    _retry_messages.append({"role": "assistant", "content": reply})
                    _retry_result = await asyncio.wait_for(
                        self.router.route(
                            task_type, _retry_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            user_openid=user_openid, session_id=session_id,
                        ),
                        timeout=15,
                    )
                    _retry_text = ""
                    if isinstance(_retry_result, str):
                        _retry_text = _retry_result
                    else:
                        _retry_text = getattr(_retry_result.choices[0].message, "content", "") or ""
                    _retry_text = self._clean_reply(_retry_text)
                    if _retry_text and len(_retry_text) > 5:
                        # P0 修复：同上，merge_continuation 已在模块顶部从 utils.llm_cleanup 导入
                        _merged, _action = merge_continuation(
                            reply, _retry_text, context="verification_no_finish_retry",
                            assume_tail=True)
                        if _action != "discarded":
                            reply = _merged
                            logger.info("verification.no_finish_retry_success",
                                        original_len=len(_reply_rstripped),
                                        final_len=len(reply), action=_action)
                            _reply_considered_complete = True
                        else:
                            # 重试重复 = LLM 认为回复已完成，视为完整
                            _reply_considered_complete = True
                    else:
                        # CodeRabbit #3 配套修复：no_finish_retry 返回空 = LLM 确认回复已完成
                        # 标记为完整，避免 force_close 追加 "。" 产生丑陋输出（保留功能性）
                        # 根因：原实现仅 log warning 不标记完整 → 落入 force_close 分支
                        # 追加 "。" → 产生 "X。" 丑陋输出（用户反馈"截断问题"的子因）
                        #
                        # CodeRabbit 复审反对：认为空重试不是正面完成证据，应保持 False
                        # 让 force_close 兜底处理。权衡：
                        # - 空 = LLM 无续写内容 → 回复实际已完成（功能正确）
                        # - force_close 追加 "。" → 产生丑陋输出（用户体验下降）
                        # - 优先保证功能性：空重试 > force_close，故保持 True
                        # - 若 force_close 行为改善（不再追加"。"），可改为 False
                        logger.info("verification.no_finish_retry_empty_confirmed_complete",
                                    finish_reason=_finish_reason, reply_len=len(reply),
                                    note="empty_retry_means_llm_confirmed_no_continuation")
                        _reply_considered_complete = True
                except Exception as e:
                    logger.warning("verification.no_finish_retry_failed",
                                   error=str(e)[:200], finish_reason=_finish_reason)
            elif _finish_reason == "length":
                # length 截断但回复太短无法重试
                logger.warning("verification.length_too_short_to_retry",
                               reply_len=len(reply), finish_reason=_finish_reason)
            # 最终兜底：仅当未判定完整时才处理
            # P0 修复：不再盲目追加 "。" —— 这会产生 "💕。" 丑陋输出
            # 改为：仅在回复不以合法标记结尾且较长时追加 "。"（极端兜底）
            if not _reply_considered_complete:
                if not reply.strip():
                    reply = DEGRADED_REPLY
                    logger.warning("verification.empty_after_leak_strip_degraded")
                else:
                    _final_rstripped = reply.rstrip()
                    # P0 修复：使用 emoji 感知的结尾判定
                    # 如果回复已以 emoji/标点结尾，不再追加 "。"
                    if not ends_with_valid_ending(_final_rstripped):
                        # 仅在确实不以任何合法标记结尾时才追加 "。"
                        # 这是最后的兜底，避免完全无标点的裸文本
                        reply = _final_rstripped + "。"
                        logger.warning("verification.incomplete_force_closed", final_len=len(reply))
                    else:
                        # 以合法标记结尾（emoji/标点），无需追加
                        reply = _final_rstripped

            return reply, []

        # ── 验收循环 ─────────────────────────────────────────
        last_tool_calls = current_tool_calls  # 追踪最近一次 tool_calls，供 summarize 使用
        for turn_idx in range(self.MAX_VERIFICATION_TURNS):
            # 墙钟超时检查
            elapsed = time.time() - loop_start
            if elapsed > self.VERIFICATION_WALL_TIMEOUT:
                trace.warning("verification.wall_timeout", turn=turn_idx, elapsed=round(elapsed, 1))
                break

            # 执行工具（skip_summarize=True：不 summarize，不更新上下文）
            _, turn_tool_results = await self._handle_tool_calls(
                current_tool_calls, messages, trace,
                assistant_content=current_assistant_content,
                reasoning_content=current_reasoning,
                user_openid=user_openid, session_id=session_id,
                safe_mode=not is_owner, ctx=ctx,
                skip_summarize=True,
            )
            all_tool_results.extend(turn_tool_results)
            last_tool_calls = current_tool_calls  # 记录本次执行的 tool_calls

            # 累计本轮搜索类工具调用次数（治本：防不收敛）
            _turn_search_calls = sum(
                1 for tc in current_tool_calls
                if tc.get("function", {}).get("name", "") in _SEARCH_TOOLS
            )
            if _turn_search_calls:
                search_tool_call_count += _turn_search_calls

            # 连续失败检查
            turn_failed = all(not r.success for r in turn_tool_results)
            if turn_failed:
                consecutive_failures += 1
                if consecutive_failures >= self.MAX_CONSECUTIVE_TOOL_FAILURES:
                    trace.warning("verification.max_failures", failures=consecutive_failures)
                    break
            else:
                consecutive_failures = 0

            # 治本：搜索类工具调用达上限后，强制禁用工具，让 LLM 基于已有结果回答。
            # 否则 LLM 会反复搜索不收敛（实测 6 轮全在搜索）。
            _effective_tools = tools
            if search_tool_call_count >= _MAX_SEARCH_CALLS and tools:
                trace.info("verification.search_cap_reached_force_answer",
                           search_calls=search_tool_call_count, cap=_MAX_SEARCH_CALLS)
                logger.info("verification.search_cap_reached search_calls={} cap={} → force answer",
                            search_tool_call_count, _MAX_SEARCH_CALLS)
                _effective_tools = None

            # 再次调用 LLM 并解析结果（返回 early_reply 时表示验收通过）
            current_tool_calls, current_assistant_content, current_reasoning, early_reply = \
                await self._call_and_parse_verification_llm(
                    messages, _effective_tools, task_type, temperature, max_tokens,
                    user_openid, session_id, trace, turn_idx, loop_start,
                )
            if early_reply is not None:
                return early_reply, all_tool_results

            if current_tool_calls is None:
                # LLM 调用失败或超时
                break

            trace.info("verification.loop", turn=turn_idx + 1,
                       tool_calls=[tc["function"]["name"] for tc in current_tool_calls])

        # ── 循环结束：最终 summarize ─────────────────────────
        # P0 修复：传入 messages（verification loop 已构建的完整上下文，含工具结果 role=tool 消息），
        # 让 _summarize_results 复用上下文而非凭空 summarize，避免工具调用后 LLM 瞎扯
        _loop_elapsed = round(time.time() - loop_start, 1)
        _total_tools = len(all_tool_results)
        if turn_idx >= self.MAX_VERIFICATION_TURNS - 1:
            logger.warning("verification.max_iterations_reached",
                           max_turns=self.MAX_VERIFICATION_TURNS,
                           elapsed=_loop_elapsed, total_tools=_total_tools)
        else:
            logger.info("verification.loop_complete",
                        turns=turn_idx + 1, elapsed=_loop_elapsed,
                        total_tools=_total_tools)
        return await self._finalize_verification_reply(
            user_input, all_tool_results, last_tool_calls or [],
            current_assistant_content, trace, user_openid, session_id,
            messages=messages,
        )

    async def _process_impl(self, ctx: RequestContext, user_input: str, user_id: str,
                             source: str, user_openid: str, session_id: str,
                             status_callback: Any, image_data: list[dict] | None,
                             is_master: bool = True,
                             system_context: str = "") -> ProcessResult:
        # P0 新增：system_context 注入（主动问候等内部场景）
        # 存储在 self 上供 _build_main_messages 使用，不写入 conversation_logs
        # P0-2 修复：同时写入 ContextVar，实现 asyncio.Task 级隔离，避免单例并发覆写。
        # 保留 self._system_context 赋值作为向后兼容（无其他读取点，但避免意外断裂）。
        self._system_context = system_context or ""
        _system_context_var.set(system_context or "")
        # 前端模式标记解析（搜索/深度思考）—— UI 按钮产生 [Search:...]/[Think:...] 标记，
        # 后端解析后剥离标记并注入对应能力：Search→强制 web_search 工具指令，Think→升级 chat_pro
        # P0 修复：不再重写 user_input，避免污染 conversation_logs.user_message
        # 根因：原实现 user_input = f"请使用 web_search 工具搜索最新信息后回答：{_sq}"
        #       导致 DB 历史记录出现"请使用 web_search 工具..."等系统指令，
        #       LLM 在后续轮次回应这些元指令，造成上下文污染。
        # 修复：剥离 marker 后保留用户原话，模式指令走 system message 注入。
        self._think_mode = False
        self._search_mode = False
        _mode_system_hint = ""  # 模式指令系统提示（不入库）
        if isinstance(user_input, str):
            _stripped_ui = user_input.strip()
            _m_search = re.match(r'^\[Search:\s*(.+?)\]\s*$', _stripped_ui)
            if _m_search:
                _sq = _m_search.group(1)
                self._search_mode = True
                # 保留用户原话，不重写 user_input
                user_input = _sq
                # 模式指令走 system message（仅 LLM 可见，不入库）
                _mode_system_hint = "本次回复请优先使用 web_search 工具搜索最新信息后回答。"
            else:
                _m_think = re.match(r'^\[Think:\s*(.+?)\]\s*$', _stripped_ui)
                if _m_think:
                    self._think_mode = True
                    user_input = _m_think.group(1)
                    _mode_system_hint = "本次回复请进行更深入的思考，可以分步骤推理。"
            # P0 新增（Task 1.9）：文档上传标记解析
            # 前端上传文档后追加 [Doc: /path/to/file] 标记
            # 后端剥离标记，注入 system message 提示 LLM 使用 document_reader 工具
            _m_doc = re.search(r'\n?\[Doc:\s*([^\]]+)\]\s*', user_input)
            if _m_doc:
                _doc_path = _m_doc.group(1).strip()
                # 从 user_input 中剥离 [Doc:] 标记（不污染历史记录）
                user_input = user_input.replace(_m_doc.group(0), "").strip()
                _doc_hint = f"用户上传了文档：{_doc_path}。请使用 document_reader 工具读取该文档内容后回答用户的问题。"
                _mode_system_hint = (_mode_system_hint + "\n" + _doc_hint).strip() if _mode_system_hint else _doc_hint
                logger.info("agent.doc_marker_parsed", doc_path=_doc_path)
        # 模式指令合并到 system_context（与主动问候等场景共用同一通道）
        if _mode_system_hint:
            self._system_context = (self._system_context + "\n" + _mode_system_hint).strip() if self._system_context else _mode_system_hint
            # P0-2：同步到 ContextVar，保持与实例属性一致
            _system_context_var.set(self._system_context)
        # 初始化 + 安全检查 + 上下文恢复
        _stage_t0 = time.time()
        trace, session_id, allowed, reason = await self._init_and_restore_context(
            ctx, user_input, user_id, source, status_callback, user_openid, session_id)
        _stage_restore_ms = int((time.time() - _stage_t0) * 1000)
        if _stage_restore_ms > 1000:
            logger.warning(f"agent.stage_slow stage=init_restore elapsed_ms={_stage_restore_ms}")
        if not allowed:
            trace.warning("agent.blocked", reason=reason)
            return ProcessResult(reply="")

        # XP 自动加成 + 用户画像统计（fire-and-forget，绝不阻塞主消息流程）
        # 治本修复（2026-08-05 用户"10秒内响应"铁证）：
        #   日志 10:10:18 XPSystem.load → 10:10:25 router.decision，中间 7s 空白。
        #   根因：XPSystem 单例首次调用触发 _load()，从 USB 盘（KIOXIA）
        #   读取 xp_state.json（26 用户）同步 IO 耗时 7s。原实现
        #   `await asyncio.gather(asyncio.to_thread(add_chat_xp), asyncio.to_thread(record_interaction))`
        #   主流程 await 线程池任务，7s 阻塞主消息流程（非事件循环，但 coroutine 卡住）。
        #   USB 盘 IO 慢是硬件限制无法根治，只能不让主流程等它。
        # 治本：fire-and-forget _spawn，XP 记录在后台跑，主流程立即继续。
        #   首次加载 7s 在后台完成，后续对话 XP 已在内存（<1ms）。
        #   XP 状态丢失不影响回复生成（仅影响等级显示），优先保证响应速度。
        #   配合 lifespan 预热（web/server.py），首次对话时 XP 已加载完毕。
        try:
            from core.xp_system import get_xp_system
            from core.user_profile_learner import get_user_profile_learner
            _xp_uid = user_openid or user_id
            if _xp_uid:
                _xp = get_xp_system()
                _learner = get_user_profile_learner()
                _is_deep = len(user_input) > 100
                # fire-and-forget：XP + profile 记录后台跑，不阻塞主流程
                _spawn(asyncio.gather(
                    asyncio.to_thread(_xp.add_chat_xp, _xp_uid, len(user_input)),
                    asyncio.to_thread(
                        _learner.record_interaction, _xp_uid, len(user_input), is_deep=_is_deep),
                ), timeout=20)
                # 周期性触发 LLM 认知抽取（不阻塞，spawn 后台）
                if _learner.should_run_insight(_xp_uid):
                    _xp_state = _xp.get_state(_xp_uid)
                    _lv = _xp_state.level.value if hasattr(_xp_state.level, 'value') else int(_xp_state.level)
                    # _run_profile_insight 仅 LLM 调用 + 写 USER.md 文件，无 DB 事务，
                    # 可安全中断，故显式传 timeout=45 防止卡死阻塞事件循环
                    _spawn(self._run_profile_insight(_xp_uid, _lv), timeout=45)
        except Exception as _e:
            logger.warning("xp.profile.record_failed", error=str(_e))

        # slash 命令
        if self.slash_handler and self.slash_handler.is_slash_command(user_input):
            slash_reply = await self.slash_handler.handle(user_input, user_id)
            return ProcessResult(reply=slash_reply)

        # G1: 问候短路（在 chat_targets 之前，跳过 LLM，<100ms 返回）
        greeting_result = self._try_greeting_shortcut(user_input, user_id, source or "")
        if greeting_result is not None:
            trace.info("agent.greeting_shortcut_hit", keyword=user_input[:20])
            return greeting_result

        # reunion_reflection: 用户"回来了"检测（离开超 30min 生成个性化重聚欢迎）
        reunion_result = await self._try_reunion_greeting(user_input, user_id, user_openid)
        if reunion_result is not None:
            trace.info("agent.reunion_greeting_hit", keyword=user_input[:20])
            return reunion_result

        _stage_t1 = time.time()
        chat_targets = await self._parse_chat_target(user_input, user_id)
        clean_input = ChatProcessor.clean_mention_from_input(user_input)

        voice_intent = self._detect_voice_intent(clean_input)
        if voice_intent == "off":
            self.set_voice_mode(False)
            force_voice = False
        elif voice_intent == "on":
            force_voice = not self._voice_mode
        else:
            force_voice = False

        if not clean_input:
            target_name = get_agent_display_name(chat_targets[0]) if chat_targets else get_agent_display_name('xiaoda')
            confirm_msg = f"好～现在跟{target_name}说话啦！有什么想聊的呀？"
            trace.info("agent.chat_target_switch", target=chat_targets)
            return ProcessResult(reply=confirm_msg, emotion="greeting")

        non_xiaoda_targets = [t for t in chat_targets if t != "xiaoda"]
        if non_xiaoda_targets:
            if len(non_xiaoda_targets) == 1:
                return await self._dispatch_single_sub_agent(
                    non_xiaoda_targets[0], clean_input, user_id, source, session_id, trace,
                    force_voice=force_voice, ctx=ctx,
                )
            return await self._dispatch_parallel_sub_agents(
                non_xiaoda_targets, clean_input, user_id, source, session_id, trace,
                force_voice=force_voice, ctx=ctx,
            )

        # P0 修复：取消 fastpath 机制（用户明确要求"取消fastpath机制，通道分类性价比太低了"）
        # 根因：fastpath 把天气/时间等需要工具的问题误判为"简单闲聊"，跳过工具调用 → 瞎扯。
        #       通道分类（simple vs complex）本身不可靠，且 fast_path 无 tools/memory/verification，
        #       导致上下文割裂和工具缺失。取消后所有消息统一走主路径（有完整工具+记忆+验收）。
        # 任务图路由也一并取消（通道分类性价比低，所有消息走主路径由 LLM 自行决定是否调工具）
        # think/search 模式仍正常工作（通过 system_context 注入模式提示，不影响主路径）

        # 主处理路径：完整记忆检索 + LLM 调用 + 后处理（统一入口，不再分流）
        _stage_t2 = time.time()
        result = await self._run_main_process_path(
            ctx, user_input, clean_input, user_id, source, user_openid, session_id,
            status_callback, image_data, is_master, force_voice, chat_targets, trace)
        _stage_main_ms = int((time.time() - _stage_t2) * 1000)
        if _stage_main_ms > 5000:
            _pre_ms = int((_stage_t2 - _stage_t1) * 1000)
            logger.warning(f"agent.stage_slow stage=main_path elapsed_ms={_stage_main_ms} pre_main_ms={_pre_ms} restore_ms={_stage_restore_ms}")

        return result

    async def _init_and_restore_context(self, ctx: Any, user_input: Any, user_id: Any, source: Any,
                                         status_callback: Any, user_openid: Any, session_id: Any) -> tuple:
        """初始化 trace、发送状态提示、安全检查、恢复用户上下文。

        返回 (trace, session_id, allowed, reason)。
        """
        if self._tool_call_handler:
            self._tool_call_handler._tool_repair.clear_storm_window()

        _trace_id = f"{int(time.time()*1000)%1000000:06d}"
        trace = logger.bind(trace_id=_trace_id)
        _proc_id = f"{user_id[:12]}@{_trace_id}"
        trace.info("agent.process.start", source=source, user_id=user_id,
                    msg_preview=user_input[:80])

        allowed, reason = self.security.is_allowed(user_id)

        # 上下文恢复阶段（详细计时日志，排查隐性超时）
        _restore_t0 = time.time()
        logger.info("pipeline.restore.start proc_id={}", _proc_id)

        # 群聊 session 按用户隔离：不同用户使用不同 session_id
        # 保留原始 session_id 作为后缀，避免上层传入的值完全丢失
        if source == "qq_group" and user_openid:
            _orig_suffix = session_id.rsplit(":", 1)[-1] if session_id else ""
            session_id = f"qq_group:{user_openid}:{_orig_suffix}" if _orig_suffix else f"qq_group:{user_openid}"

        # 按当前用户恢复历史摘要（群聊多用户上下文隔离）
        # P0 修复（用户反馈"对话链路阻塞"根因）：
        # switch_user_context 和 restore_from_db 曾因数据库连接竞争/锁等待阻塞 38 秒
        # （日志 17:23:16 agent.process.start → 17:23:54 context.restored）。
        # 修复：给两步分别加超时，超时后降级跳过（宁可上下文不完整也不阻塞主流程）。
        # P0-1 修复（QQ 会话恢复键与写库键不一致 → 突然失忆）：
        # 写库键为 qq_{openid}（qq_bot_adapter.py:628），恢复必须用同一 user_id，
        # 否则 restore_from_db 用裸 openid 查询 → DB 返回 0 行 → 每次重启后完全失忆。
        _restore_id = user_id or user_openid
        if _restore_id:
            try:
                await asyncio.wait_for(
                    self.context.switch_user_context(_restore_id),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("agent.switch_user_context_timeout",
                               timeout=5.0, user_id=_restore_id,
                               hint="锁竞争或事件循环阻塞，跳过用户切换")
            except Exception as e:
                logger.warning("agent.switch_user_context_failed", error=str(e))
        if _restore_id and self.db:
            try:
                await asyncio.wait_for(
                    self.context.restore_from_db(
                        self.db, user_id=_restore_id,
                        address_term=self.context.current_address_term),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("agent.restore_from_db_timeout",
                               timeout=10.0, user_id=_restore_id,
                               hint="数据库查询阻塞，跳过历史摘要恢复")
            except Exception as e:
                logger.warning("agent.restore_failed", error=str(e))

        logger.info("pipeline.restore.done proc_id={} elapsed_ms={}",
                    _proc_id, int((time.time() - _restore_t0) * 1000))
        return trace, session_id, allowed, reason

    def _try_greeting_shortcut(self, user_input: str, user_id: str, source: str) -> ProcessResult | None:
        """G1: 纯问候短路 - 跳过 LLM 直接返回 <100ms.

        - 默认开启（ENABLE_GREETING_SHORTCUT=true）
        - 群聊跳过（避免刷屏）
        - 问候不超过 20 字符才命中（避免"你好帮我写代码"误命中）
        - 感谢类返回随机感谢回复，其他返回时段问候

        Returns:
            ProcessResult | None: 命中返回 ProcessResult(emotion="greeting")，否则 None
        """
        if not _is_greeting_enabled():
            return None
        # 群聊跳过（避免刷屏）
        if source and "group" in source.lower():
            return None
        text = (user_input or "").strip()
        if not text or len(text) > 20:  # 问候不超过 20 字符
            return None
        match = _GREETING_PATTERN.match(text)
        if not match:
            return None
        keyword = match.group(1).lower()
        # 时段问候（5-12 早上好 / 12-18 下午好 / 18-22 晚上好 / 22-5 夜深）
        # G1: 使用 Asia/Shanghai 时区，避免受系统时区影响
        now_hour = datetime.now(_SH_TZ).hour
        reply = None
        for start_h, end_h, msg in _TIME_GREETINGS:
            if start_h <= now_hour < end_h:
                reply = msg
                break
        if reply is None:
            reply = msg  # fallback 到最后一个（覆盖 0-5 点）
        # 感谢类覆盖时段问候
        if keyword in ("谢谢", "感谢", "thanks", "thx", "多谢"):
            reply = random.choice(_THANK_REPLIES)
        # P1-6 + CodeRabbit F4: 语音模式下问候也要走 TTS 路径，但必须与
        # _build_voice_result 的 5 条件对齐：voice_mode + tts.available +
        # TTS_ASYNC_MODE + is_feature_available("tts") + len(reply) > 2
        # 避免在 TTS 不可用/降级模式/同步模式下无效设 tts_pending
        # TTS 时机控制 v2：统一过 _decide_tts_trigger（移除冷却，改为内容适宜性守卫）
        if (TTS_ASYNC_MODE
                and _decide_tts_trigger(
                    reply, force_voice=False, voice_mode=self._voice_mode,
                    tts_available=self.tts.available,
                    tts_enabled=get_degradation_strategy().is_feature_available("tts"))):
            return ProcessResult(
                reply=reply, emotion="greeting",
                tts_pending=True, tts_text=reply,
            )
        return ProcessResult(reply=reply, emotion="greeting")

    async def _try_reunion_greeting(self, user_input: str, user_id: str,
                                    user_openid: str) -> ProcessResult | None:
        """reunion_reflection 接线：用户"回来了"检测，生成个性化重聚欢迎。

        - 仅命中"回来了/我回来了"等关键词时触发
        - idle 时长从最近 session 的 ended_at 计算，last_emotion 从 mental_state 读取
        - 任何异常回退 None，不阻塞主流程（后续仍走正常 LLM 路径）
        """
        text = (user_input or "").strip()
        if not text or len(text) > 20:
            return None
        if not _REUNION_PATTERN.search(text):
            return None
        try:
            # 1. idle_seconds：从最近 session 的 ended_at 计算
            idle_seconds = 0.0
            _uid = user_openid or user_id
            if _uid and getattr(self, "db", None) is not None:
                try:
                    cursor = await self.db._conn.execute(
                        "SELECT ended_at FROM sessions WHERE user_openid=? "
                        "ORDER BY ended_at DESC LIMIT 1",
                        (_uid,),
                    )
                    row = await cursor.fetchone()
                    if row and row[0]:
                        idle_seconds = max(0.0, time.time() - float(row[0]))
                except Exception:
                    idle_seconds = 0.0
            # 2. last_emotion：从 mental_state 的 user_last_emotion 读取
            last_emotion = ("neutral", 0.0)
            try:
                from core.mental_state import get_mental_state_manager_if_exists
                mgr = get_mental_state_manager_if_exists(user_id=user_id)
                if mgr is not None:
                    _label = getattr(mgr.state.S, "user_last_emotion", "")
                    if _label:
                        last_emotion = (_label, 0.5)
            except Exception as e:
                logger.debug("reunion_reflection.last_emotion_failed", error=str(e))
            # 3. 生成重聚欢迎消息（内部有降级模板，router 缺失也能返回）
            from emotion.reunion_reflection import generate_reunion_message
            reply = await generate_reunion_message(
                idle_seconds=idle_seconds,
                last_emotion=last_emotion,
                router=getattr(self, "router", None),
                address_term=getattr(self.context, "current_address_term", "爸爸"),
            )
            return ProcessResult(reply=reply, emotion="greeting")
        except Exception:
            logger.debug("reunion_reflection.failed")
            return None

    async def _run_main_process_path(self, ctx: Any, user_input: Any, clean_input: Any, user_id: Any, source: Any,
                                      user_openid: Any, session_id: Any, status_callback: Any, image_data: Any,
                                      is_master: Any, force_voice: Any, chat_targets: Any, trace: Any) -> Any:
        """主处理路径：完整记忆检索 + LLM 调用 + 后处理。"""
        _pipeline_t0 = time.time()
        _proc_id = f"{user_id[:12]}@{int(_pipeline_t0 * 1000) % 100000}"

        # 记忆检索阶段
        _mp_t0 = time.time()
        logger.info("pipeline.memory.start proc_id={} user_id={}", _proc_id, user_id[:20])
        emotion, emotion_label = await self._setup_main_emotion_and_memory(
            user_input, clean_input, chat_targets, is_master, ctx)
        _mp_memory_ms = int((time.time() - _mp_t0) * 1000)
        logger.info("pipeline.memory.done proc_id={} elapsed_ms={} emotion={}",
                    _proc_id, _mp_memory_ms, emotion_label)
        if _mp_memory_ms > 3000:
            logger.warning(f"agent.stage_slow stage=memory_retrieval elapsed_ms={_mp_memory_ms}")

        # 消息构建阶段
        _mp_t1 = time.time()
        logger.info("pipeline.build_msg.start proc_id={}", _proc_id)
        messages, _pre_picked_sticker, tools = await self._build_main_messages(
            user_input, is_master, image_data, clean_input, emotion, user_id, source)
        _mp_build_ms = int((time.time() - _mp_t1) * 1000)
        logger.info("pipeline.build_msg.done proc_id={} elapsed_ms={} msg_count={} tool_count={}",
                    _proc_id, _mp_build_ms, len(messages), len(tools) if tools else 0)
        if _mp_build_ms > 2000:
            logger.warning(f"agent.stage_slow stage=build_messages elapsed_ms={_mp_build_ms}")

        # 任务类型解析与熔断器检查
        _mp_t1b = time.time()
        early_result, task_type, _cb_max_tokens, circuit_state, _model_cfg = \
            self._resolve_task_and_circuit(user_input, tools, messages, trace, source=source)
        logger.info("pipeline.resolve_task.done proc_id={} elapsed_ms={} task_type={} circuit={}",
                    _proc_id, int((time.time() - _mp_t1b) * 1000), task_type, circuit_state)
        if early_result is not None:
            logger.info("pipeline.early_return proc_id={} reason=circuit_or_task", _proc_id)
            return early_result

        # 主 LLM 调用 + 验收循环
        _mp_t2 = time.time()
        logger.info("pipeline.llm_verify.start proc_id={} task_type={} max_tokens={}",
                    _proc_id, task_type, _cb_max_tokens)
        principal = getattr(ctx, "principal", None)
        is_owner = principal.is_owner if principal is not None else ctx.is_master
        reply, tool_results = await self._call_main_llm_with_verification(
            messages, tools, task_type, _model_cfg, _cb_max_tokens, circuit_state,
            status_callback, user_openid, session_id, trace, ctx, user_input, is_owner)
        _mp_llm_ms = int((time.time() - _mp_t2) * 1000)
        logger.info("pipeline.llm_verify.done proc_id={} elapsed_ms={} reply_len={} tool_results={}",
                    _proc_id, _mp_llm_ms, len(reply) if reply else 0,
                    len(tool_results) if tool_results else 0)
        if _mp_llm_ms > 5000:
            logger.warning(f"agent.stage_slow stage=llm_verify elapsed_ms={_mp_llm_ms} memory_ms={_mp_memory_ms} build_ms={_mp_build_ms}")

        # 后处理阶段（含媒体提取与隐私扫描）
        _mp_t3 = time.time()
        logger.info("pipeline.finalize.start proc_id={}", _proc_id)
        _result = await self._finalize_main_reply(
            reply, tool_results, user_input, user_id, source, emotion,
            emotion_label, ctx, user_openid, is_master, _pre_picked_sticker, force_voice, trace,
            session_id)
        logger.info("pipeline.finalize.done proc_id={} elapsed_ms={} total_ms={}",
                    _proc_id, int((time.time() - _mp_t3) * 1000),
                    int((time.time() - _pipeline_t0) * 1000))
        return _result

    async def _setup_main_emotion_and_memory(self, user_input: Any, clean_input: Any,
                                               chat_targets: Any, is_master: Any,
                                               ctx: Any) -> tuple:
        """主路径阶段1：Klee 委托 + 情绪检测 + 记忆检索。返回 (emotion, emotion_label)。"""
        # IP-safe: 动态读取 xiaoli 的 display_name，避免硬编码原名
        from config import get_agent_display_name
        _xiaoli_dn = get_agent_display_name("xiaoli")
        _xiaoli_names = {"可莉", "小莉", _xiaoli_dn, "xiaoli"}
        if any(n in user_input for n in _xiaoli_names) and "xiaoda" in chat_targets:
            klee_reply = await self.delegate_to_klee(clean_input, factual=True)
            self.context.klee_context = klee_reply
        else:
            self.context.klee_context = None

        emotion = detect_emotion(user_input)
        # emotion_llm 后台 fire-and-forget（不阻塞主流程，结果异步更新 mental_state）
        try:
            from config import ENABLE_EMOTION_LLM
            if ENABLE_EMOTION_LLM:
                _spawn(self._run_emotion_llm_background(
                    user_input, getattr(ctx, "user_id", "")), timeout=2.0)
        except Exception:
            logger.debug("emotion.llm_spawn_failed")
        emotion_hint = build_emotion_hint(emotion)
        self.context.emotion_hint = emotion_hint
        ctx.last_user_emotion = emotion.get("primary", "")
        self._update_mental_state_emotion(emotion, user_id=getattr(ctx, "user_id", ""))

        # 记忆检索与 notebook 上下文加载并行化
        memories = await self._retrieve_main_memories(user_input, is_master, emotion)
        self.context.memory_retrieval = memories if memories else None

        emotion_label = emotion.get("primary", "")
        return emotion, emotion_label

    async def _run_emotion_llm_background(self, user_input: str, user_id: str) -> None:
        """emotion_llm 后台深度情绪分析（fire-and-forget）。

        不阻塞主流程，LLM 结果异步写入 mental_state（primary + PAD + needs），
        使后续请求的情绪引导提示使用更精准的 LLM 情绪信息。任何异常均吞掉。
        """
        try:
            from emotion.emotion_llm import detect_emotion_llm
            llm_emotion = await detect_emotion_llm(
                user_input, router=getattr(self, "router", None))
            if not llm_emotion or not llm_emotion.get("primary"):
                return
            from core.mental_state import get_mental_state_manager_if_exists
            mgr = get_mental_state_manager_if_exists(user_id=user_id)
            if mgr is not None and mgr.enabled:
                _pad = None
                if all(k in llm_emotion for k in ("P", "A", "D")):
                    _pad = {"P": llm_emotion["P"], "A": llm_emotion["A"],
                            "D": llm_emotion["D"]}
                _needs = llm_emotion.get("needs") or None
                mgr.update_short_term(
                    emotion="",
                    user_emotion=llm_emotion.get("primary", ""),
                    user_pad=_pad,
                    user_needs=_needs,
                )
                logger.debug("emotion.llm_background_updated",
                             primary=llm_emotion.get("primary"))
        except Exception:
            logger.debug("emotion.llm_background_failed")

    async def _build_main_messages(self, user_input: Any, is_master: Any, image_data: Any,
                                     clean_input: Any, emotion: Any,
                                     user_id: Any, source: Any = None) -> tuple:
        """主路径阶段2：构建消息 + 图片描述注入 + 表情包/工具准备。返回 (messages, _pre_picked_sticker, tools)。"""
        # 构建消息
        effective_input = user_input
        if not is_master:
            safe_prompt = build_safe_system_prompt(
                address_term=self.context.current_address_term)
            messages = [{"role": "system", "content": safe_prompt}]
            messages.append({"role": "user", "content": effective_input})
        else:
            messages = await self.context.build_messages(effective_input, source=source or "")

        # P0 新增：system_context 注入（主动问候等内部场景）
        # 根因：nudge_engine/greeting_scheduler 原先把场景提示作为 user_input 传入，
        #       导致 conversation_logs.user_message 出现"（场景：现在早上...）"等系统提示，
        #       污染历史记录 + LLM 在后续轮次回应这些元提示。
        # 修复：场景提示走 system message，user_input 保持中性占位符（如"（主动问候）"），
        #       仅 LLM 可见，不写入 DB。
        # P0-2 修复：从 ContextVar 读取（Task 级隔离），而非实例属性（单例并发覆写）
        _sys_ctx = _system_context_var.get() or ""
        if _sys_ctx:
            # 插入到消息列表的开头（system prompt 之后、history 之前）
            _sys_msg = {"role": "system", "content": _sys_ctx}
            # 找到第一个非 system 消息的位置，插入到其前面
            _insert_idx = 0
            for i, m in enumerate(messages):
                if m.get("role") != "system":
                    _insert_idx = i
                    break
                _insert_idx = i + 1
            messages.insert(_insert_idx, _sys_msg)
            logger.debug("agent.system_context_injected",
                         ctx_len=len(_sys_ctx), insert_pos=_insert_idx)

        # 图片描述注入
        messages = await self._inject_image_description(messages, user_input, image_data)

        # 表情包意图与工具准备
        _pre_picked_sticker, tools = self._prepare_sticker_and_tools(
            messages, clean_input, emotion, is_master, user_id, user_input, image_data,
            source=source or "")

        return messages, _pre_picked_sticker, tools

    async def _finalize_main_reply(self, reply: str, tool_results: Any, user_input: Any,
                                     user_id: Any, source: Any, emotion: Any,
                                     emotion_label: str, ctx: Any, user_openid: Any,
                                     is_master: Any, _pre_picked_sticker: Any,
                                     force_voice: Any, trace: Any, session_id: Any) -> ProcessResult:
        """主路径阶段4+5：媒体提取、隐私扫描、人格校验、上下文记录、情绪标签、语音构建。返回 ProcessResult。"""
        # 媒体提取与隐私扫描
        media_image_paths, media_video_path, reply = await self._extract_media_from_tool_results(
            tool_results, reply)
        # 兜底：提取 LLM 伪造的图片 URL（未调 agnes_image_generate 而在回复里写 markdown 图/裸 URL）
        fab_image_paths, reply = await self._extract_fabricated_images_from_reply(reply)
        media_image_paths.extend(fab_image_paths)
        if not is_master and reply:
            safe, alt_reply, _ = self.security.check_output_privacy(reply)
            if not safe:
                logger.warning("agent.privacy_leak_blocked", user_id=user_id, reply_preview=reply[:100])
                reply = alt_reply

        # Persona Critic: 检查 LLM 输出人格一致性（LLM 输出后、发送给用户前）
        self._apply_persona_critic(reply, user_openid, user_id)

        # 仅主人群聊消息（及非群聊场景）记入记忆
        _should_remember = is_master or source != "qq_group"
        if _should_remember:
            if not ctx.handled_by_tool_call:
                # 降级/错误回复既不入记忆库也不入对话历史，
                # 否则 build_messages() 会让 LLM 在后续轮次看到系统内部状态，
                # 导致 LLM 模仿降级语气或基于假历史编造上下文。
                # 同时跳过 user 消息，避免留下未配对的 user 消息造成上下文断档。
                if is_degraded_reply(reply):
                    logger.info("agent.skip_degraded_reply_not_in_history", source=source, reply_preview=reply[:60])
                else:
                    await self.context.add_message("user", user_input)
                    rc = self.router.pop_reasoning_content()
                    # strip emotion tags before storing to memory
                    _clean_for_memory = self.sticker_manager.strip_emotion_tag(reply)
                    await self.context.add_message("assistant", _clean_for_memory, reasoning_content=rc)
                    # L5 修复: 捕获本次回复使用的模型名，透传到 conversation_logs.model_used
                    _model_used = self.router.get_current_chat_model().get("model_id", "")
                    self._bg_task_manager.run_background_tasks(
                        user_input, _clean_for_memory, user_id, source, emotion, tool_results,
                        session_id=session_id, model_used=_model_used)
        # 偏好管线: 用户纠正 → L1(约束) + L3(教训) 联动 (异步, 不阻塞回复)
        try:
            from core.preference_pipeline import get_preference_pipeline
            _spawn(get_preference_pipeline().process_correction(
                user_input, reply, self._bg_task_manager.learning_manager))
        except Exception as e:
            logger.debug("msg.preference_pipeline_spawn_failed", error=str(e))
        try:
            _spawn(self.router.flush_costs())
        except Exception as e:
            logger.error("费用统计刷新失败，可能丢失费用数据: {}", str(e))

        trace.info("agent.process.done", reply_preview=reply[:100],
                   reply_len=len(reply))

        # 情绪标签
        if is_unified():
            reply, ensured_emotion = ensure_emotion_tag(reply)
            if ensured_emotion.value != emotion_label:
                emotion_label = ensured_emotion.value

        if _pre_picked_sticker:
            clean_reply = self._finalize_reply(reply, strip_emotion=True, style="xiaoda")
            sticker_path = _pre_picked_sticker
        else:
            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
            # 统一清洗出口（get_sticker_info 已剥 emotion tag，故 strip_emotion=False）
            clean_reply = self._clean_reply_full(clean_reply, style="xiaoda", strip_emotion=False)

        audio_path, tts_pending, tts_text = await self._build_voice_result(
            clean_reply, emotion_label, force_voice)
        if audio_path:
            clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"

        _spawn(self._hook_engine.fire_post_response())

        # 更新持续情绪状态（让 agent 有情绪惯性）
        try:
            from emotion.emotion_state import get_emotion_state
            _intensity_map = {
                "happy": 0.6, "excited": 0.8, "love": 0.7,
                "shy": 0.5, "sad": 0.7, "angry": 0.8,
                "surprised": 0.7, "confused": 0.4, "thinking": 0.3,
                "playful": 0.6, "moved": 0.7, "anxious": 0.6,
                "fear": 0.8, "pout": 0.5, "neutral": 0.2,
            }
            get_emotion_state(getattr(ctx, "user_id", "")).update(
                emotion_label, _intensity_map.get(emotion_label, 0.5)
            )
        except Exception as e:
            logger.debug("emotion_state.update_failed", error=str(e))

        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
                             audio_path=audio_path, tool_results=tool_results, image_paths=media_image_paths,
                             video_path=media_video_path, tts_pending=tts_pending, tts_text=tts_text)

    def _dynamic_emotion_threshold(self, user_input: str, emotion: dict, base: float = 0.5) -> float:
        """根据对话情景动态调整情绪触发阈值。

        自适应策略:
          1. 情绪强度高 → 降低阈值 (更容易触发安慰记忆)
          2. 用户表达情感关键词多 → 降低阈值
          3. 对话深入 (长输入) → 降低阈值
          4. 短/无情感输入 → 保持或提高阈值 (避免误触发)

        最终阈值 clamp 在 [0.2, 0.8] 范围内, 防止极端值。
        """
        threshold = base
        intensity = float(emotion.get("intensity", 0.0))

        # 因子 1: 情绪强度越高, 阈值越低
        # intensity 0.8 → threshold -= 0.15; intensity 0.3 → threshold += 0.05
        if intensity >= 0.7:
            threshold -= 0.15
        elif intensity >= 0.5:
            threshold -= 0.05
        elif intensity <= 0.2:
            threshold += 0.05

        # 因子 2: 情感关键词密度
        emotional_words = (
            "难过", "伤心", "哭", "痛", "累", "烦", "压力", "焦虑",
            "害怕", "孤独", "想你", "分手", "吵架", "遗憾", "后悔",
            "开心", "喜欢", "幸福", "感恩", "想", "心情", "感觉",
        )
        query_lower = user_input.lower() if isinstance(user_input, str) else ""
        emo_count = sum(1 for w in emotional_words if w in query_lower)
        if emo_count >= 3:
            threshold -= 0.1   # 密集情感表达 → 大幅降低
        elif emo_count >= 1:
            threshold -= 0.05  # 有情感词 → 小幅降低

        # 因子 3: 输入长度 (深入对话)
        effective_len = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in query_lower)
        if effective_len > 40:
            threshold -= 0.05  # 长输入: 用户在认真倾诉

        return max(0.2, min(0.8, threshold))

    async def _retrieve_main_memories(self, user_input: Any, is_master: Any, emotion: Any) -> Any:
        """主路径记忆检索（含情绪触发的安抚记忆）与 notebook 加载并行。"""
        _retrieve_start = time.time()
        # 记忆检索超时（秒）在此统一解析一次，内层检索与外层 gather 复用同一值，
        # 避免配置 MEMORY_RETRIEVE_TIMEOUT > 8 时被外层写死的 8s 硬顶（日志口径不一致）。
        import config as _cfg
        _mem_timeout = float(getattr(_cfg, "MEMORY_RETRIEVE_TIMEOUT", 8.0))

        async def _retrieve_memories() -> Any:
            # 降级检查: L2+ 关闭记忆检索, 跳过以减少负载
            if not get_degradation_strategy().is_feature_available("memory_search"):
                return None
            if self.memory and is_master:
                self.memory.signal_new_message()
                _t0 = time.time()
                logger.info("pipeline.memory.retrieve.start")
                try:
                    _k = self.memory._suggest_k(user_input, default_k=8)
                    logger.info("pipeline.memory.retrieve.call start k={}", _k)
                    # 治本（2026-08-05）：单次记忆检索超时 2→5s。
                    # 根因：2s 对 embed/reranker/检索链路过短，网络波动即误砍，
                    #       导致 memory.retrieve_timeout_single 频繁 → 记忆注入为空 → 回复短。
                    # 记忆检索已与 notebook/constraint 解耦并独立执行。
                    # (2026-08-06) 超时改为 config.MEMORY_RETRIEVE_TIMEOUT（默认 8s）：
                    #   USB 盘慢时 5s 仍频繁误砍（今日 66 次 retrieve_timeout_single），
                    #   8s 给慢速存储足够余量，同时控制最坏延迟（LLM 前串行 await）。
                    from memory.scope import current_scope
                    memory_scope = current_scope()
                    results = await asyncio.wait_for(
                        self.memory.retrieve_memories(
                            user_input,
                            k=_k,
                            scope=memory_scope,
                            conv_user_id=memory_scope.user_id,
                        ),
                        timeout=_mem_timeout,
                    )
                    _retrieve_ms = int((time.time() - _t0) * 1000)
                    logger.info("pipeline.memory.retrieve.done elapsed_ms={} result_count={}",
                                _retrieve_ms, len(results) if results else 0)
                except asyncio.TimeoutError:
                    _delay = (time.time() - _t0) - _mem_timeout
                    logger.warning("memory.retrieve_timeout_single",
                                   hint=f"单次记忆检索超时 {_mem_timeout}s，跳过本次记忆",
                                   cancel_delay_ms=int(_delay * 1000),
                                   query_preview=user_input[:50])
                    results = None
                except Exception as e:
                    from local_ai.integration.reranker import is_structured_local_unavailable

                    if is_structured_local_unavailable(e):
                        raise
                    logger.warning("memory.retrieve_failed", error=str(e))
                    results = None
                if results is not None:
                    # 动态情绪阈值: 根据对话情景自适应调整
                    _base_threshold = 0.5
                    try:
                        import config as _cfg
                        _base_threshold = float(getattr(_cfg, "EMOTION_TRIGGER_THRESHOLD", 0.5))
                    except (ImportError, ValueError, TypeError):
                        pass
                    _emo_threshold = self._dynamic_emotion_threshold(
                        user_input, emotion, _base_threshold
                    )
                    # 注：comfort_memories 不再追加到 results
                    # 根因：retrieve_comfort_memories 只按情绪标签+重要性+时间排序，
                    # 与当前 query 零语义相关，会污染记忆检索结果，导致"回忆不准"。
                    # 情绪安抚应由模型基于真实相关记忆自行组织语言，而非注入无关"开心记忆"。
                    return results
                return None
            return None

        async def _load_notebook() -> None:
            _t0 = time.time()
            logger.info("pipeline.memory.notebook.start")
            try:
                await self._load_notebook_context()
                _elapsed = int((time.time() - _t0) * 1000)
                logger.info("pipeline.memory.notebook.done elapsed_ms={}", _elapsed)
                if _elapsed > 500:
                    logger.warning("memory.notebook_load_slow",
                                   elapsed_ms=_elapsed)
            except Exception as e:
                logger.warning("notebook.load_failed", error=str(e))

        # 治本（2026-08-05）：核心记忆检索与次要上下文解耦，杜绝"记忆被整段跳过"。
        # 根因：原实现 asyncio.gather(记忆检索, notebook, constraint) 整体被 3s wait_for
        #       包裹。notebook 加载慢（USB 盘 IO，实测 8s）时，3s 超时取消整个 gather，
        #       连正在进行的核心记忆检索也一并取消 → memory.retrieve_global_timeout →
        #       记忆被跳过 → 注入"没有找到相关记忆" → 回复短而敷衍。
        # 修复（根治，非缩短超时掩盖）：
        #   1) 核心记忆检索独立执行，给足超时（8s），确保能完整执行并注入上下文；
        #   2) notebook 是次要信息（当前关注点/待办），转后台异步，慢不阻塞记忆检索；
        #   3) constraint_lessons 结果从未被消费（jianjia 子串匹配粗糙，见下方注释），
        #      整体移除该空转慢环节。
        _gather_start = time.time()
        memories_task = asyncio.create_task(_retrieve_memories())
        _spawn(_load_notebook())  # 后台异步，不占用记忆检索关键路径
        try:
            memories = await asyncio.wait_for(memories_task, timeout=_mem_timeout)
        except asyncio.TimeoutError:
            _cancel_delay = (time.time() - _gather_start) - _mem_timeout
            logger.warning("memory.retrieve_global_timeout",
                           hint=f"记忆检索整体超时 {_mem_timeout}s，跳过记忆继续生成回复",
                           cancel_delay_ms=int(_cancel_delay * 1000),
                           query_preview=user_input[:50])
            memories = None
        logger.info("memory.retrieve_stage",
                    stage="gather_done",
                    elapsed_ms=int((time.time() - _gather_start) * 1000),
                    has_memories=bool(memories))

        # 注：constraint_lessons 不再追加到 memories
        # 根因：jieba 子串匹配粗糙，单关键词命中即入选，极易注入无关经验，
        # 且以 "[经验] xxx" 格式混入 memory_retrieval，污染记忆检索结果。
        # 经验教训应通过独立通道（如 volatile 层）注入，不混入"相关记忆"。

        # ContextNest A2: 审计本次响应消费了哪些记忆版本 (point-in-time 重建支持)
        if memories and hasattr(self.memory, "audit_retrieval"):
            try:
                from memory.context_governance import ContextGovernance
                _response_id = ContextGovernance.new_response_id()
                # 治本修复（2026-08-05）：audit_retrieval 改 fire-and-forget。
                # 根因：audit_retrieval 写 DB（USB 盘），await 阻塞主流程 9s
                # （日志 11:37:11→11:37:20 memory.audited 9s 铁证）。
                # 审计是后台数据操作，不影响回复生成，无需阻塞用户等待。
                _spawn(self.memory.audit_retrieval(_response_id, memories))
            except Exception as e:
                logger.debug("memory.audit_call_failed", error=str(e))

        return memories

    async def _inject_image_description(self, messages: Any, user_input: Any, image_data: Any) -> Any:
        """向 messages 注入图片描述（直接传入图片或从用户输入提取路径）。"""
        if image_data:
            logger.info("agent.vision_start", image_count=len(image_data),
                        total_b64_size=sum(len(img.get('data', '')) for img in image_data))
            image_description = await self._describe_images(image_data)
            if image_description:
                messages.append({
                    "role": "system",
                    "content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n请用你自己的语气和人格风格，自然地向用户描述你看到了什么，不要直接复述识别结果，不要提及视觉模型或识别工具。"
                })
            else:
                messages.append({
                    "role": "system",
                    "content": "用户发送了一张图片，但视觉识别未能成功识别图片内容。请诚实地告诉用户你暂时看不清这张图片，不要编造图片内容，可以请用户描述一下图片里是什么。"
                })
        elif "[图片:" in user_input and "已保存到" in user_input:
            img_path_match = re.search(r'已保存到\s+([^\s，。]+)', user_input)
            if img_path_match:
                img_path = img_path_match.group(1)
                # 路径安全检查：仅允许项目目录和临时目录
                _allowed_prefixes = (
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    os.path.expanduser("~"),
                    "/tmp",
                    tempfile.gettempdir(),
                )
                _resolved = os.path.realpath(os.path.abspath(img_path))
                if not any(_resolved.startswith(p) for p in _allowed_prefixes):
                    logger.warning("chat.image_path_traversal_blocked", path=img_path)
                    messages.append({
                        "role": "system",
                        "content": "[系统提示] 用户发送了一张图片，但图片路径不合法。请告诉用户你暂时无法查看这张图片。"
                    })
                else:
                    try:
                        mime, img_b64 = encode_image_to_base64(img_path)
                        image_description = await self._describe_images([{"mimeType": mime, "data": img_b64}])
                        if image_description:
                            messages.append({
                                "role": "system",
                                "content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n请用你自己的语气和人格风格，自然地向用户描述你看到了什么，不要直接复述识别结果，不要提及视觉模型或识别工具。"
                            })
                        else:
                            messages.append({
                                "role": "system",
                                "content": "用户发送了一张图片，但视觉识别未能成功识别图片内容。请诚实地告诉用户你暂时看不清这张图片，不要编造图片内容，可以请用户描述一下图片里是什么。"
                            })
                    except (FileNotFoundError, ValueError):
                        messages.append({
                            "role": "system",
                            "content": "[系统提示] 用户发送了一张图片，但图片文件无法读取。请告诉用户你暂时无法查看这张图片。"
                        })
                    except Exception as e:
                        logger.warning("agent.image_load_failed", error=str(e))
                        messages.append({
                            "role": "system",
                            "content": "[系统提示] 用户发送了一张图片，但图片加载失败。请告诉用户你暂时无法查看这张图片。"
                        })
        return messages

    def _prepare_sticker_and_tools(self, messages: Any, clean_input: Any, emotion: Any, is_master: Any,
                                    user_id: Any, user_input: Any, image_data: Any,
                                    source: str = "") -> tuple:
        """准备表情包提示（注入 messages）与工具列表。返回 (_pre_picked_sticker, tools)。"""
        _sticker_keywords = ["表情包", "表情", "贴纸", "sticker", "贴图"]
        _sticker_intent = any(kw in clean_input for kw in _sticker_keywords)
        _pre_picked_sticker = None
        if (_sticker_intent and self.sticker_manager.available
                and get_degradation_strategy().is_feature_available("emotion")):
            _detected_e = self.sticker_manager.detect_emotion(clean_input)
            if not _detected_e:
                _detected_e = CN_TO_EN.get(emotion.get("primary", ""), "happy")
            _pre_picked_sticker = self.sticker_manager.pick(_detected_e)
            if _pre_picked_sticker:
                _sticker_desc = _pre_picked_sticker.stem.split("_", 1)[-1].replace("_", " ").replace("-", " ")
                _sticker_cat = _detected_e
                messages.append({
                    "role": "system",
                    "content": f"[系统提示] 你正在给用户发送一张表情包图片。图片描述：「{_sticker_desc}」，情绪分类：「{_sticker_cat}」。请在回复中自然地提到这张表情包的内容，让用户感受到你真的知道发了什么图。不要说'这是一张图片'之类的机械描述，要用你的风格自然表达。"
                })

        _tools_list = to_openai_tools()
        tools = _tools_list if _tools_list else None
        # 有图片时也保留完整工具列表——用户可能发参考图+要求生成图片（图生图场景），
        # 禁用工具会导致 agnes_image_generate 无法调用，LLM 只能用文字"假装"已生成。
        # 让 LLM 自行决定是否调用工具，符合下方"统一保留完整工具列表"的原则。

        # P0 修复（用户明确要求"取消对话通道分类机制"）：
        # 移除 filter_tools_for_simple_task 调用——通道分类性价比太低，
        # 误判会导致工具被错误过滤（如天气查询被当简单闲聊→工具被移除→瞎扯）。
        # 所有消息统一保留完整工具列表，由 LLM 自行决定是否调用。
        # 表情包意图时硬移除 delegate_task（CLAUDE.md 规范）
        # 表情包由主体流程自动附带，委托出去会丢失预选表情包和 system message
        if _sticker_intent and tools:
            tools = [t for t in tools if t.get("function", {}).get("name") != "delegate_task"]
            if not tools:
                tools = None
        # 非主人消息：白名单制，只保留允许的工具
        if not is_master and tools:
            tools = [t for t in tools if t.get("function", {}).get("name") in self.ALLOWED_NON_MASTER_TOOLS]
            if not tools:
                tools = None
            logger.info("agent.tools_filtered_for_non_master",
                        user_id=user_id, source=source,
                        allowed=list(self.ALLOWED_NON_MASTER_TOOLS))
        return _pre_picked_sticker, tools

    def _resolve_task_and_circuit(self, user_input: Any, tools: Any, messages: Any, trace: Any,
                                    source: str = "qq") -> tuple:
        """任务类型解析与熔断器检查。返回 (early_result, task_type, _cb_max_tokens, circuit_state, _model_cfg)。

        early_result 非 None 时表示熔断器 RED 状态，应直接返回。
        Web UI (source="web") 使用更高的 max_tokens 以支持近似 Hermes 的长回复流式输出；
        QQ 通道保持 ROUTE_TABLE 默认值（平台消息长度有限制）。
        """
        should_escalate, reason = self._should_escalate_to_pro(user_input, tools)
        # chat_pro 已合并进 chat（agnes 不支持 thinking，升级无意义）
        base_task = "chat"
        task_type = self.router.resolve_task_type(base_task)
        if should_escalate:
            trace.info("chat.escalate_skipped_merged", reason=reason,
                       hint="chat_pro merged into chat, agnes disables thinking")

        _model_cfg = AGENT_CONFIG.get("model", {})
        circuit_state = self._circuit_breaker.check(self._cognitive_state)
        if circuit_state == CircuitState.RED:
            logger.warning("agent.circuit_breaker_red")
            return ProcessResult(reply="系统需要休息一下，请稍后再试吧～"), \
                task_type, None, circuit_state, _model_cfg
        if circuit_state == CircuitState.HALF_OPEN:
            logger.info("agent.circuit_breaker_half_open_probe")

        _cb_max_tokens = None
        # Web UI 近似 Hermes 无限流式输出：使用 131072 tokens 上限（匹配模型上下文窗口）
        # P0 重构（用户明确要求"不许截断"）：
        # 根因：32768 对中文长回复过小，频繁触发 finish_reason="length"，
        #       原"请继续完成你的回复"重试 prompt 会污染上下文（LLM 回应"继续完成"等元词汇）。
        # 修复：提升到 131072（mimo/agnes 上下文窗口 128K），从源头消除截断。
        #       即使模型偶尔生成超长回复，也由 agent_context 压缩机制处理，不再截断重试。
        # QQ 通道保持 None → 走 ROUTE_TABLE 默认值（1500），避免超长回复被 QQ 平台截断
        _web_max_tokens = _safe_int(os.getenv("WEB_UI_MAX_TOKENS", "131072"), 131072)
        if source == "web":
            _cb_max_tokens = _web_max_tokens
        if circuit_state == CircuitState.YELLOW:
            messages.append({
                "role": "system",
                "content": "[系统警告] 当前认知状态不佳，请简化回复。"
            })
            _base_mt = _cb_max_tokens if _cb_max_tokens else _model_cfg.get("max_tokens", DEFAULT_MAX_TOKENS)
            _cb_max_tokens = int(_base_mt * 0.8)
        return None, task_type, _cb_max_tokens, circuit_state, _model_cfg

    async def _call_main_llm_with_verification(self, messages: Any, tools: Any, task_type: Any, _model_cfg: Any,
                                                _cb_max_tokens: Any, circuit_state: Any, status_callback: Any,
                                                user_openid: Any, session_id: Any, trace: Any, ctx: Any, user_input: Any, is_owner: Any) -> tuple:
        """主 LLM 调用 + 验收循环 + 熔断器状态更新。返回 (reply, tool_results)。"""
        reply = ""
        tool_results = []
        try:
            _llm_t0 = time.time()
            if STREAM_TEXT_PUSH and status_callback and not tools:
                logger.info("pipeline.llm_call.start mode=stream task_type={}", task_type)
                result = await self._stream_llm_response(
                    messages, status_callback=status_callback, task_type=task_type,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    user_openid=user_openid, session_id=session_id,
                )
            else:
                logger.info("pipeline.llm_call.start mode=route task_type={} timeout={}",
                            task_type, self.LLM_CALL_TIMEOUT)
                result = await asyncio.wait_for(self.router.route(
                    task_type, messages,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    user_openid=user_openid, session_id=session_id,
                ), timeout=self.LLM_CALL_TIMEOUT)
            logger.info("pipeline.llm_call.done elapsed_ms={} result_type={}",
                        int((time.time() - _llm_t0) * 1000),
                        "str" if isinstance(result, str) else "tool_calls")

            # Harness 验收循环
            _verify_t0 = time.time()
            reply, tool_results = await self._run_verification_loop(
                result, messages, tools, trace,
                task_type=task_type,
                temperature=_get_temperature(_model_cfg),
                max_tokens=_cb_max_tokens,
                user_openid=user_openid, session_id=session_id,
                is_owner=is_owner, ctx=ctx, user_input=user_input,
            )
            logger.info("pipeline.verification.done elapsed_ms={} reply_len={} tool_count={}",
                        int((time.time() - _verify_t0) * 1000),
                        len(reply) if reply else 0, len(tool_results) if tool_results else 0)
            if tool_results:
                ctx.handled_by_tool_call = True
            # 最终防线：如果 verification loop 返回空回复，触发 fallback
            if not reply or not reply.strip():
                logger.warning("agent.empty_reply_guard", tool_count=len(tool_results))
                raise RuntimeError("empty_reply_guard: verification loop 返回空回复")
            logger.info("agent.got_reply", length=len(reply), preview=reply[:80],
                        tool_count=len(tool_results))
            # ── 跨对话回复去重（模型层面去重机制） ──
            # 检测新回复与最近 N 条回复的相似度，超阈值则重试一次
            # P0 修复：移除 `not tool_results` 门控条件
            # 根因：诊断日志证实"在吗"类问候回复 tool_cnt=1（情绪/贴纸工具）被跳过去重，
            #   而数据库里"在的呢～""像被电流贯穿"等重复恰是问候/角色扮演回复——
            #   这类最易重复的回复却被排除在去重之外，导致去重对最严重场景完全失效。
            # reply 是 LLM 最终回复文本，与是否调用工具无关，应统一参与去重。
            if reply and len(reply) > 20:
                _dedup_t0 = time.time()
                logger.info("pipeline.dedup.start reply_len={}", len(reply))
                reply = await self._dedup_reply_against_recent(
                    reply, messages, task_type, _model_cfg,
                    _cb_max_tokens, user_openid, session_id, trace,
                )
                logger.info("pipeline.dedup.done elapsed_ms={} reply_len={}",
                            int((time.time() - _dedup_t0) * 1000), len(reply))
            if circuit_state == CircuitState.HALF_OPEN:
                self._circuit_breaker.on_half_open_success(self._cognitive_state)
            else:
                self._circuit_breaker.on_success(self._cognitive_state)
        except Exception as e:
            trace.error("agent.model_error", error=str(e))
            if circuit_state == CircuitState.HALF_OPEN:
                self._circuit_breaker.on_half_open_failure(self._cognitive_state)
            else:
                self._circuit_breaker.on_failure(self._cognitive_state)
            if self._error_handler:
                try:
                    error_reply = await self._error_handler.handle_error_with_intelligence(
                        error=e, user_query=user_input, context="主处理流程模型调用错误"
                    )
                    reply = error_reply if error_reply and len(error_reply) > 50 else DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.error_handler_fallback: {e}")
                    reply = DEGRADED_REPLY
            else:
                try:
                    result = await self.router.route(
                        "chat", messages, temperature=0.7,
                        user_openid=user_openid, session_id=session_id,
                    )
                    reply = self._clean_reply(result) if isinstance(result, str) else DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.flash_fallback: {e}")
                    reply = DEGRADED_REPLY
        return reply, tool_results

    def _dedup_buf(self, user_id: str) -> list[str]:
        """获取用户的去重缓冲（LRU 维护 + 上限淘汰）。

        根因修复（用户反馈"每段对话80%一样"）：去 key 从 session_id 改为 user_id。
          - 微信 adapter 根本没传 session_id（空串）→ 原 key 退化为 user_openid
          - QQ c2c 的 session_id 每小时换一次（SES-YYYYMMDD-XXXXX）→ key 频繁失效
            → 内存缓存命中失败 → 去重对最易重复的场景完全失效
          - 改用 user_id（wechat_{openid} / qq_{openid}），跨 session 稳定，重启前不换
        LRU 维护不变：OrderedDict + move_to_end + popitem(last=False) 上限淘汰，
        防止长期运行内存泄漏（原 session_id 含日期每天新增 key 的根因）。
        """
        _dd = self._recent_replies
        buf = _dd.setdefault(user_id, [])
        _dd.move_to_end(user_id)
        while len(_dd) > self.REPLY_DEDUP_SESSION_CAP:
            _dd.popitem(last=False)
        return buf

    async def _dedup_reply_against_recent(
        self, reply: str, messages: Any, task_type: Any, _model_cfg: Any,
        _cb_max_tokens: Any, user_openid: Any, session_id: Any, trace: Any,
    ) -> str:
        """跨对话回复去重：检测新回复与最近回复的相似度，重复则重试一次。

        根因修复（用户反馈"每段对话80%一样"，且要求"重试后相似度必须 <70%，只允许重试一次"）：
          1. 去 key 从 session_id 改为 user_id（稳定标识）：
             - 微信 adapter 不传 session_id（空串）→ 原 key 退化为 user_openid
             - QQ c2c session_id 每小时换（SES-YYYYMMDD-XXXXX）→ 内存缓存频繁失效
             → 改用 user_id（wechat_{openid}/qq_{openid}），跨 session 稳定
          2. 持久化去重：从 conversation_logs 查最近回复，替代易失内存缓存：
             - 服务重启后内存清空 → 去重历史丢失 → 相同输入生成相同回复
             - 从 DB 按 user_id 查询，确保重启后/换 session 后去重状态不丢失
          3. 内存缓存仍保留作为同进程快速路径：DB 写入是 fire-and-forget，
             同进程内连续请求时 DB 可能还没写入，内存缓存补足这个时序窗口

        机制：
        1. recent = 内存缓存 ∪ 数据库最近回复（合并去重，最新在前）
        2. 新回复与之比较 rapidfuzz 相似度
        3. 超阈值则追加 system message 要求"完全不同的表达"重试一次
        4. 重试后仍 >=70% → 返回相似度最低的版本（用户要求只重试一次，不无限重试）
        5. 无论用哪个，都更新内存缓存（DB 由 background_tasks 写入）
        """
        from utils.similarity import ratio as text_ratio

        # 根因修复：用 user_id（稳定标识）作为去 key，替代不稳定的 session_id
        ctx = _current_request_ctx.get()
        _user_id = getattr(ctx, "user_id", "") or user_openid or "_default"
        _source = getattr(ctx, "source", "") or ""

        # 1. 合并内存缓存 + 数据库最近回复（持久化去重）
        # 治本（2026-08-05）：用户明确要求"去重只跟上一条消息对比，不是跟全部历史对比"。
        # 根因：原先与最近 5 条历史逐一对比，用户反复发相似消息（"在吗""我要亲亲"）时
        #   agnes 生成相似回复 → 高相似度 → 触发去重重试 → 第二次 LLM 调用 → 总耗时 20s+。
        # 修复：只取最近 1 条回复对比（limit=1），从源头消除"与多条历史重复"的误判面，
        #   从而大幅降低触发重试的概率，保持主 LLM 调用单次 8s 内的健康耗时。
        mem_recent = self._dedup_buf(_user_id)  # 内存缓存（LRU 维护）
        db_recent: list[str] = []
        if self.db and _user_id != "_default":
            try:
                db_recent = await asyncio.wait_for(
                    self.db.get_recent_replies(_user_id, source=_source,
                                               limit=1),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                logger.warning("reply.dedup_db_timeout user_id={}", _user_id[:24])
            except Exception as e:
                logger.warning("reply.dedup_db_failed error={}", str(e)[:200])

        # 合并：内存缓存 + DB（去重，保持最新在前，只保留最近 1 条用于对比）
        _seen: set[str] = set()
        recent: list[str] = []
        for r in list(mem_recent) + db_recent:
            if r and r not in _seen:
                _seen.add(r)
                recent.append(r)
        recent = recent[:1]

        logger.info(f"reply.dedup_probe | user={_user_id[:24]} | "
                    f"mem_cnt={len(mem_recent)} | db_cnt={len(db_recent)} | "
                    f"merged_cnt={len(recent)} | reply_preview={reply[:40]}")

        # 无历史回复，直接记录并返回
        if not recent:
            _buf = self._dedup_buf(_user_id)
            _buf.append(reply)
            if len(_buf) > self.REPLY_DEDUP_MAX:
                del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
            return reply

        # 计算与最近回复的最大相似度
        max_sim = max(text_ratio(reply, r) for r in recent)
        logger.info(f"reply.dedup_check | user={_user_id[:20]} | "
                    f"max_sim={max_sim:.1f} | merged_cnt={len(recent)}")

        if max_sim < self.REPLY_DEDUP_THRESHOLD:
            # 不重复，记录并返回（保持最近 N 条）
            _buf = self._dedup_buf(_user_id)
            _buf.append(reply)
            if len(_buf) > self.REPLY_DEDUP_MAX:
                del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
            return reply

        # 重复了，重试一次
        trace.warning("reply.duplicate_detected",
                      max_similarity=round(max_sim, 1),
                      recent_count=len(recent),
                      preview=reply[:60])

        try:
            # 治本：重试时只传 system + 最近 2 轮历史，不传完整历史。
            # 根因是模型看到历史里的重复回复跟风——截断历史让模型没有重复参考，
            # 从源头防止生成重复回复。历史仍在数据库，主路径不受影响。
            _retry_messages = [messages[0]] if messages else []  # system prompt
            _retry_messages += messages[-4:]  # 最近 2 轮（user+assistant 各 2）
            _retry_messages += [{
                "role": "system",
                "content": (
                    f"你刚才的回复与之前说过的内容相似度高达{max_sim:.0f}%，"
                    "几乎是一模一样的话。请用完全不同的措辞、句式和描写角度重新回复，"
                    "不要重复之前用过的任何描写（如'像被电流贯穿''手指死死抓着床单'等），"
                    "换一种全新的表达方式。"
                ),
            }]
            # 尊重 WebUI temperature 设定，不篡改（用户明确要求不许自动调整 temperature）
            _retry_result = await asyncio.wait_for(
                self.router.route(
                    task_type, _retry_messages,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    user_openid=user_openid, session_id=session_id,
                ),
                timeout=self.REPLY_DEDUP_RETRY_TIMEOUT,
            )
            _retry_reply = ""
            if isinstance(_retry_result, str):
                _retry_reply = self._clean_reply(_retry_result)
            else:
                _retry_reply = getattr(
                    _retry_result.choices[0].message, "content", "") or ""
                _retry_reply = self._clean_reply(_retry_reply)

            if _retry_reply and len(_retry_reply) > 20:
                _retry_sim = max(text_ratio(_retry_reply, r) for r in recent)
                if _retry_sim < self.REPLY_DEDUP_THRESHOLD:
                    # 重试成功：相似度 <70%，用重试回复
                    _buf = self._dedup_buf(_user_id)
                    _buf.append(_retry_reply)
                    if len(_buf) > self.REPLY_DEDUP_MAX:
                        del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
                    logger.info("reply.dedup_retry_ok retry_sim={:.1f}", _retry_sim)
                    return _retry_reply
                # 重试后仍 >=70%（用户要求"重试后必须 <70%，不然就是bug"）
                # 只允许重试一次，取相似度较低的版本作为兜底，并告警便于排查
                if _retry_sim < max_sim:
                    reply = _retry_reply
                    max_sim = _retry_sim
                trace.warning("reply.dedup_retry_still_duplicate",
                              retry_sim=round(_retry_sim, 1),
                              threshold=self.REPLY_DEDUP_THRESHOLD,
                              hint="重试后仍超阈值，取相似度最低版本兜底")
        except asyncio.TimeoutError:
            logger.warning("reply.dedup_retry_timeout timeout={}",
                           self.REPLY_DEDUP_RETRY_TIMEOUT)
        except Exception as e:
            logger.warning("reply.dedup_retry_failed error={}", str(e)[:200])

        _buf = self._dedup_buf(_user_id)
        _buf.append(reply)
        if len(_buf) > self.REPLY_DEDUP_MAX:
            del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
        return reply

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

    def _parse_verification_result(self, current_result: Any, tools: list[dict] | None) -> tuple:
        """从 LLM 输出中解析 tool_calls、assistant_content、reasoning。"""
        current_tool_calls = None
        current_assistant_content = ""
        current_reasoning = None
        if isinstance(current_result, str):
            if has_dsml_tool_calls(current_result) and tools:
                dsml_calls = parse_dsml_tool_calls(current_result, self.tool_repair._allowed_tools)
                if dsml_calls:
                    current_tool_calls = dsml_calls
                    current_assistant_content = current_result
                    current_reasoning = self.router.pop_reasoning_content()
        else:
            msg = current_result.choices[0].message
            if msg.tool_calls:
                current_tool_calls = [
                    {"id": str(tc.id), "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": str(tc.function.arguments) if tc.function.arguments else "{}"}}
                    for tc in msg.tool_calls
                ]
                current_assistant_content = msg.content or ""
                current_reasoning = getattr(msg, "reasoning_content", None)
                self.router.pop_reasoning_content()
        return current_tool_calls, current_assistant_content, current_reasoning

    async def _call_and_parse_verification_llm(self, messages: Any, tools: Any, task_type: Any, temperature: Any,
                                                max_tokens: Any, user_openid: Any, session_id: Any, trace: Any,
                                                turn_idx: Any, loop_start: Any) -> tuple:
        """验收循环中再次调用 LLM 并解析结果。

        返回 (tool_calls, content, reasoning, early_reply)。
        early_reply 非 None 时表示验收通过可直接返回；
        tool_calls 为 None 时表示调用失败或超时应退出循环。
        """
        remaining = self.VERIFICATION_WALL_TIMEOUT - (time.time() - loop_start)
        if remaining < 3:
            trace.warning("verification.no_time_left")
            return None, "", None, None

        try:
            current_result = await asyncio.wait_for(
                self.router.route(
                    task_type, messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    user_openid=user_openid,
                    session_id=session_id,
                ),
                timeout=min(self.LLM_CALL_TIMEOUT, remaining),
            )
        except TimeoutError:
            trace.warning("verification.llm_timeout", turn=turn_idx)
            return None, "", None, None
        except Exception as e:
            trace.error("verification.llm_error", turn=turn_idx, error=str(e))
            return None, "", None, None

        current_tool_calls, current_assistant_content, current_reasoning = \
            self._parse_verification_result(current_result, tools)

        # 如果没有 tool_calls，验收通过
        if not current_tool_calls:
            if isinstance(current_result, str):
                early_reply = self._clean_reply(current_result)
            else:
                early_reply = self._clean_reply(current_result.choices[0].message.content or "")
            # 关键修复：空回复不应被视为验收通过
            # 根因：LLM 在工具调用后可能返回空 content（finish_reason=stop + content=""），
            # clean_reply 后为空字符串。"" is not None 导致 verification loop 直接返回空回复给用户。
            if not early_reply or not early_reply.strip():
                trace.warning("verification.empty_reply_after_tools", turn=turn_idx)
                return None, "", None, None  # signal failure → 走 _finalize_verification_reply
            # 截断兜底：循环重试直到回复完整或达到最大重试次数，确保用户永不看到截断
            # P0 修复（emoji 感知）：原检查只认 ASCII/CJK 标点，角色扮演回复常以 emoji 结尾
            # （如 "💕"、"😳💗"），被误判不完整 → 追加 "。" → 产生 "💕。" 丑陋输出。
            # 改用 ends_with_valid_ending() 包含 emoji 判定，避免 false-positive 截断。
            from utils.llm_cleanup import has_english_reasoning_leak as _early_has_eng_leak
            from utils.llm_cleanup import strip_english_reasoning_leak as _early_strip_eng_leak
            _early_considered_complete = False
            # 获取工具后回复的 finish_reason（用于完整性判定）
            _early_finish_reason = None
            if not isinstance(current_result, str):
                _early_finish_reason = getattr(current_result.choices[0], "finish_reason", None)
            else:
                _early_finish_reason = _stream_finish_reason_var.get()
            # 重试机制保留：工具调用后回复不完整时续写，最多 3 次重试
            # P0 修复（阻塞根因 2026-08-04）：重试循环必须受墙钟硬约束
            # 根因：原实现每次重试用固定 timeout=LLM_CALL_TIMEOUT(30s) 且不检查剩余时间，
            #   3 次重试最多 90s，远超 VERIFICATION_WALL_TIMEOUT(50s)。
            #   日志实测 llm_verify 阶段 74-89s（86752/74908/89574ms），
            #   导致 main_path 97s + wechat_bot.process_timeout / qq_bot.c2c_timeout 连锁超时。
            # 修复：每轮重试前计算墙钟剩余时间，<3s 则立即停止重试接受当前回复；
            #   单次重试超时取 min(LLM_CALL_TIMEOUT, remaining)，绝不超出墙钟。
            for _early_retry in range(3):
                _early_remaining = self.VERIFICATION_WALL_TIMEOUT - (time.time() - loop_start)
                if _early_remaining < 3:
                    trace.warning("verification.incomplete_retry_no_time_left",
                                  retry=_early_retry, remaining=round(_early_remaining, 1))
                    break
                _early_rstripped = early_reply.rstrip()
                _early_ends_valid = ends_with_valid_ending(_early_rstripped)
                _early_has_opening = any(kw in early_reply for kw in ["让我", "查一下", "看看", "查查", "找找"])
                _early_eng_leak = _early_has_eng_leak(early_reply)
                _early_just_cleaned = False
                if _early_eng_leak:
                    # N7: 英文推理泄漏 → 清洗后视为不完整，触发重试
                    early_reply = _early_strip_eng_leak(early_reply, context="after_tools")
                    _early_rstripped = early_reply.rstrip()
                    _early_ends_valid = ends_with_valid_ending(_early_rstripped)
                    _early_just_cleaned = True
                # 完整性判定（P0 修复：emoji 感知 + 信任 LLM finish_reason="stop"）
                # 根因：原检查只认标点，emoji 结尾被误判不完整 → 追加 "。" → "💕。"
                # 修复：
                #   1. 使用 ends_with_valid_ending() 包含 emoji 判定
                #   2. finish_reason="stop" + 长回复(>=30字符) → 信任 LLM，视为完整
                #   3. 清洗后即使有标点也视为不完整（内容被截断，需重试获取剩余部分）
                #   4. 开场白回复（"让我查一下。"等）即使 <30 字符也不能标记为完整
                # CodeRabbit #1 修复：移除独立的 `or _early_ends_valid`
                # 原 implementation 最后一行让前两个 length-specific 条件失效，
                # 导致短开场白（"让我查一下。"）即使 _early_has_opening=True 也被误判完整
                # 修复：_early_ends_valid 只在 length-specific 分支中评估
                #   - 短回复(<30)：非开场白 + 合法结尾 → 完整
                #   - 长回复(>=30)：finish_reason="stop" 或 合法结尾 → 完整（保留 finish_reason=None 信任）
                # P1-4 修复：内部场景（system_context 非空）跳过提前完成判定，
                # 避免主动问候被 force_close 或误判完整后截断。
                if _system_context_var.get():
                    _early_considered_complete = True
                    break  # 内部场景：信任 LLM 输出，不做完整性判定
                _early_complete = not _early_just_cleaned and (
                    (len(early_reply) < 30 and not _early_has_opening and _early_ends_valid)
                    or (len(early_reply) >= 30 and _early_finish_reason == "stop")
                    or (len(early_reply) >= 30 and _early_ends_valid)
                )
                if _early_complete:
                    _early_considered_complete = True
                    break  # 回复完整
                # 只有短回复开场白 或 无合法结尾长回复 或 英文泄漏时才重试
                _need_retry = (
                    (len(early_reply) < 80 and _early_has_opening)
                    or not _early_ends_valid
                    or _early_eng_leak
                    or _early_just_cleaned
                )
                if not _need_retry:
                    break  # 不符合重试条件
                trace.warning("verification.incomplete_reply_after_tools",
                              reply_len=len(early_reply), reply_preview=early_reply[:50],
                              retry=_early_retry, has_eng_leak=_early_eng_leak)
                try:
                    # P0 修复（上下文污染根因）：
                    # 原实现 messages.append({"role": "user", "content": "请继续给出具体内容..."})
                    # 把元指令当 user message 注入，LLM 在后续轮次会回应"继续给出具体内容"等元词汇，
                    # 造成上下文割裂和角色出戏（详见 conversation_logs 2026-07-25 17:46 案例）。
                    # 修复：改用 assistant-prefill —— 追加已有 early_reply 作为 assistant 消息，
                    #       让 LLM 从此处续写具体内容，不追加任何 user message。
                    #       这样元指令不进入 LLM 可见上下文，避免污染。
                    # CodeRabbit #4 修复：用副本避免污染 verification loop 共享的 messages
                    # 原 implementation 直接 messages.append()，导致 early_reply 在多次 retry 中累积
                    # 对齐 L319-323 length-retry 模式：_retry_messages = list(messages)
                    _retry_messages = list(messages)
                    _retry_messages.append({"role": "assistant", "content": early_reply})
                    # 不追加 user message —— assistant-prefill 模式让 LLM 自然续写
                    # P0 修复（阻塞根因）：超时取 min(LLM_CALL_TIMEOUT, remaining)，绝不超出墙钟
                    retry_result = await asyncio.wait_for(
                        self.router.route(
                            task_type, _retry_messages, temperature=temperature, max_tokens=max_tokens,
                            user_openid=user_openid, session_id=session_id,
                        ),
                        timeout=min(self.LLM_CALL_TIMEOUT, _early_remaining),
                    )
                    retry_reply = retry_result if isinstance(retry_result, str) else (retry_result.choices[0].message.content or "")
                    retry_reply = self._clean_reply(retry_reply)
                    if retry_reply and len(retry_reply) > 10:
                        _early_merged, _early_action = merge_continuation(
                            early_reply, retry_reply, context="after_tools_retry")
                        if _early_action == "discarded":
                            # 重试重复 = LLM 认为回复已完成，视为完整不再 force_close
                            _early_considered_complete = True
                            break  # 重试重复
                        early_reply = _early_merged
                        trace.info("verification.incomplete_retry_success_after_tools",
                                   final_len=len(early_reply), retry=_early_retry,
                                   merge_action=_early_action)
                    else:
                        break  # 重试返回空或太短
                except Exception as e:
                    trace.warning("verification.incomplete_retry_failed_after_tools", error=str(e))
                    break
            # 最终兜底：仅当 for 循环未判定完整时才处理
            # P0 修复：不再盲目追加 "。" —— 使用 emoji 感知的结尾判定
            if not _early_considered_complete:
                # CodeRabbit 复审修复：泄漏清洗后回复可能为空，用降级回复而非"。"
                if not early_reply.strip():
                    early_reply = DEGRADED_REPLY
                    trace.warning("verification.empty_after_leak_strip_degraded_after_tools")
                else:
                    _early_final = early_reply.rstrip()
                    # P0 修复：使用 emoji 感知的结尾判定
                    # 如果回复已以 emoji/标点结尾，不再追加 "。"
                    if not ends_with_valid_ending(_early_final):
                        early_reply = _early_final + "。"
                        trace.warning("verification.incomplete_force_closed_after_tools", final_len=len(early_reply))
                    else:
                        early_reply = _early_final
            return None, "", None, early_reply

        return current_tool_calls, current_assistant_content, current_reasoning, None

    async def _finalize_verification_reply(self, user_input: Any, all_tool_results: Any, last_tool_calls: Any,
                                            current_assistant_content: Any, trace: Any, user_openid: Any, session_id: Any,
                                            messages: list[dict] | None = None) -> tuple:
        """验收循环结束后生成最终回复。

        P0 修复：新增 messages 参数，传入 verification loop 已构建的完整上下文
        （含 system+history+assistant(tool_calls)+tool(result)），让 _summarize_results
        复用上下文而非凭空 summarize，避免工具调用后 LLM 瞎扯/出戏。
        """
        trace.info("verification.summarize_fallback", tool_count=len(all_tool_results))
        if all_tool_results:
            final_reply = await self._tool_call_handler._summarize_results(
                user_input, all_tool_results, last_tool_calls,
                trace, user_openid=user_openid, session_id=session_id,
                messages=messages,
                assistant_content=current_assistant_content,
            )
            # 关键修复：_summarize_results 可能返回空（LLM 再次返回空内容），兜底 DEGRADED_REPLY
            if not final_reply or not final_reply.strip():
                trace.warning("verification.summarize_empty_fallback")
                final_reply = DEGRADED_REPLY
        elif current_assistant_content.strip():
            final_reply = self._clean_reply(current_assistant_content)
        else:
            final_reply = DEGRADED_REPLY
        return final_reply, all_tool_results

    async def _stream_llm_response(self, messages: list, status_callback: Any=None,
                                    task_type: str = "chat", **kwargs: Any) -> str:
        """流式调用 LLM，逐 token 推送给前端。

        当 STREAM_TEXT_PUSH=true 时使用此方法。
        失败时降级到原有同步调用。
        """
        if not STREAM_TEXT_PUSH:
            return await self.router.route(task_type, messages, **kwargs)

        # 重置流式 finish_reason，避免上次调用的残留值干扰截断检测
        # CodeRabbit 复审修复 #6：改为 ContextVar 重置（每个 Task 有独立 context）
        _stream_finish_reason_var.set(None)
        full_response = []
        try:
            async for delta in self.router.chat_stream(messages, task_type=task_type, **kwargs):
                if delta:
                    full_response.append(delta)
                    if status_callback:
                        try:
                            await status_callback({
                                "type": "stream_text",
                                "delta": delta,
                                "accumulated": "".join(full_response),
                            })
                        except Exception as cb_err:
                            logger.debug("agent.stream_callback_failed: {}", str(cb_err)[:100])
        except Exception as e:
            logger.warning("message_processor.stream_llm_failed: {}", str(e)[:200])
            accumulated = "".join(full_response)
            # 根因：流式失败时原实现直接回落到 route()，route() 虽内部也走 fallback 链，
            # 但走的是「重新选主 provider 再失败再 fallback」的完整路径，多一次主调用开销；
            # 且 stream 路径的 e 已经是真实失败原因，直接喂给 _try_fallback_chain 跳过主重试更高效。
            # 保留 accumulated 非空时返回部分内容的现有行为，避免重复内容（已推送的 delta 不能撤回）。
            if accumulated:
                logger.info("message_processor.stream_partial_return len={}", len(accumulated))
                return accumulated + "\n\n[⚠️ 内容生成中断，以上为已生成的部分]"
            # 取舍：降级时 stream=False，把流式退化为一次性返回。
            # 原因：此处再消费一个 fallback provider 的流对象需要重复 stall timeout/finish_reason
            # 检测逻辑，复杂且易错；非流式返回用户感知仅是「这次没有逐字效果」，可靠性优先。
            fb_result = await self.router._try_fallback_chain(
                e, task_type, messages,
                kwargs.get("temperature", 0.7),
                False,
                kwargs.get("tools"),
                kwargs.get("tool_choice"),
                kwargs.get("timeout", 60),
                kwargs.get("user_openid", ""),
                kwargs.get("session_id", ""),
                kwargs.get("extra_headers"),
                original_max_tokens=kwargs.get("max_tokens"),
            )
            # 降级返回 str 直接用；返回 None（所有降级目标不可用）才回落到 route() 兜底。
            if isinstance(fb_result, str) and fb_result:
                return fb_result
            return await self.router.route(task_type, messages, **kwargs)
        return "".join(full_response)

    def _should_escalate_to_pro(self, user_msg: str, tools: list | None) -> tuple[bool, str]:
        # P0 修复（用户明确要求"取消对话通道分类机制"）：
        # 移除基于关键词/长度的通道分类（PRO_TASK_KEYWORDS）——性价比太低且误判多。
        # 仅保留显式用户意图触发：前端 [Think:] 按钮按下时升级到 chat_pro。
        # 工具调用、长消息、情感内容等不再通过关键词预判升级，
        # 由 LLM 在主路径自行决定推理深度（chat 模型已具备足够能力）。
        if getattr(self, "_think_mode", False):
            return True, "user_think_mode"
        return False, ""

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

    def _update_mental_state_emotion(self, emotion: dict, user_id: str = "") -> None:
        """将检测到的用户情绪更新到 L/M/S 心理状态模型的 S 层.

        受 MENTAL_STATE_ENABLED 环境变量控制, 默认开启.
        任何异常都被吞掉, 不影响主消息处理流程.
        """
        try:
            from core.mental_state import get_mental_state_manager
            mgr = get_mental_state_manager(user_id=user_id)
            if mgr.enabled:
                mgr.update_short_term(
                    emotion="",
                    user_emotion=emotion.get("primary", ""),
                )
        except Exception as e:
            logger.debug(f"mental_state.update_failed: {e}")

    def _apply_persona_critic(self, reply: str, user_openid: str, user_id: str) -> None:
        """应用 Persona Critic 检查 LLM 输出的人格一致性.

        在 LLM 输出后、发送给用户前调用.
        零质量回退: 任何异常都不影响主流程, 仅记录日志.
        """
        if not reply:
            return
        try:
            from core.persona_coherence import get_persona_critic
            from core.xp_system import get_xp_system

            _uid = user_openid or user_id
            if not _uid:
                return

            critic = get_persona_critic()
            if not critic.enabled:
                return

            xp_sys = get_xp_system()
            xp_state = xp_sys.get_state(_uid)
            check = critic.check(reply, xp_state.level.value)

            if check.needs_rewrite:
                logger.info("persona.rewrite_triggered",
                           score=check.score, issues=check.issues)
                # 实际重写逻辑可由调用方决定, 此处仅记录
            elif check.score < 0.7:
                # 添加案例到 Case Repository 供后续检索学习
                try:
                    critic._case_repo.add_case(reply, check)
                except Exception as e:
                    logger.debug(f"persona.add_case_failed: {e}")
        except Exception as e:
            logger.warning("persona.check_failed", error=str(e))

    async def _run_profile_insight(self, user_id: str, xp_level: int) -> None:
        """后台任务：调用 LLM 抽取用户认知并写入 USER.md。"""
        try:
            from core.user_profile_learner import get_user_profile_learner
            learner = get_user_profile_learner()

            # 从对话上下文获取近期消息
            recent = []
            try:
                recent = self.context.get_last_n(20) or []
            except Exception as e:
                logger.debug("recent_messages_read_failed", error=str(e))

            if not recent:
                return

            prompt = learner.build_insight_prompt(recent, xp_level)
            if not prompt:
                return

            # 轻量级 LLM 调用（使用 flash 路由，低成本）
            response = await self.router.route(
                task_type="memory_encoding",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=512,
                timeout=15,
            )
            if response:
                learner.save_insight(user_id, str(response), xp_level)
        except Exception as e:
            # A5 修复：使用结构化日志添加 error_type，便于排查空错误消息
            logger.warning("profile_learner.insight_failed",
                           error=str(e), error_type=type(e).__name__)

    # P0 修复（Task 1.7）：MiMo Vision API 已知失败模式
    # 当模型返回这些字符串时，说明图片识别失败，不应作为合法 description 透传
    VISION_FAILURE_PATTERNS = (
        "cannot read image", "unable to read", "i cannot read",
        "image not readable", "can't read", "无法识别",
        "图片无法识别", "图片读取失败", "无法读取图片",
    )

    async def _describe_images(self, image_data: list[dict]) -> str:
        """使用 Vision API 识别图片内容。

        P0 修复（用户要求"主chatLLM是谁图片发给谁，不要硬编mimo"）：
        - 移除硬编码 `provider="mimo"` 和 `model=MIMO_MODEL`
        - 改用 `router.get_vision_provider_and_model()` 动态选择：
          优先用当前主 chat LLM（若 supports_vision），否则从 provider_metadata.json
          找 vision-capable provider，最后兜底环境变量。
        - 保留 Task 1.6（安全客户端路径）、1.7（失败模式校验）、1.8（BadRequestError 捕获）
        """
        try:
            # Task 1.6：走安全客户端路径（含锁 + 懒注册 + LLMError）
            if not self.router:
                logger.warning("agent.vision_no_router")
                return ""
            # P0 修复：动态选择 vision provider + model（不再硬编码 mimo）
            _vision_provider, _vision_model = self.router.get_vision_provider_and_model()
            if not _vision_provider or not _vision_model:
                logger.warning("agent.vision_no_capable_provider",
                               hint="主 chat LLM 不支持 vision 且元数据无 vision-capable provider")
                return ""
            try:
                client = await self.router._select_client_for_provider(_vision_provider)
            except Exception as ce:
                logger.warning("agent.vision_client_unavailable",
                               provider=_vision_provider,
                               error=f"{type(ce).__name__}: {ce}"[:200])
                return ""
            logger.info("agent.vision_client_acquired",
                        provider=_vision_provider, model=_vision_model)

            vision_parts = [{"type": "text", "text": "请详细描述这张图片的内容。如果有文字，请完整转录。如果是题目，请给出题目内容。"}]
            for i, img in enumerate(image_data):
                b64_data = img.get('data', '')
                mime = img.get('mimeType', 'image/jpeg')
                logger.info("agent.vision_image", index=i, mime=mime, b64_len=len(b64_data))
                if not b64_data:
                    logger.warning("agent.vision_empty_data", index=i)
                    continue
                vision_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64_data}"
                    }
                })

            if len(vision_parts) <= 1:
                logger.warning("agent.vision_no_valid_images")
                return ""

            # Task 1.8：优先捕获 BadRequestError，记录具体错误码
            try:
                response = await client.chat.completions.create(
                    model=_vision_model,
                    messages=[{"role": "user", "content": vision_parts}],
                    max_tokens=1024,
                )
            except _openai_mod.BadRequestError as be:
                # vision API 的 BadRequestError 通常意味着图片格式/大小问题
                _status = getattr(be, "response", None)
                _status_code = _status.status_code if _status is not None else None
                logger.warning("agent.vision_bad_request",
                               provider=_vision_provider, model=_vision_model,
                               status_code=_status_code,
                               body=str(getattr(be, "body", ""))[:200],
                               error=f"{type(be).__name__}: {be}"[:200])
                return ""
            except _openai_mod.APIError as ae:
                logger.warning("agent.vision_api_error",
                               provider=_vision_provider, model=_vision_model,
                               error=f"{type(ae).__name__}: {ae}"[:200])
                return ""

            description = (response.choices[0].message.content or "").strip()
            logger.info("agent.image_described", length=len(description),
                        provider=_vision_provider, model=_vision_model,
                        preview=description[:80])

            # Task 1.7：校验响应内容，识别已知失败模式
            # 根因：Vision API 可能把 "cannot read image" 作为 content 返回，
            #       原实现不校验直接透传到 system message，导致主聊天 LLM 据此回答"看不清图片"
            if not description or len(description) < 10:
                logger.warning("agent.vision_suspicious_response",
                               reason="too_short", content_preview=description[:100])
                return ""
            _desc_lower = description.lower()
            for pattern in self.VISION_FAILURE_PATTERNS:
                if pattern in _desc_lower:
                    logger.warning("agent.vision_suspicious_response",
                                   reason="failure_pattern_matched",
                                   pattern=pattern,
                                   content_preview=description[:100])
                    return ""  # 走兜底分支

            return description
        except Exception as e:
            logger.warning("agent.image_describe_failed",
                           error=str(e), error_type=type(e).__name__)
            return ""

    async def _xiaoda_synthesis_chat(self, prompt: str) -> str:
        try:
            result = await self.router.route(
                "chat",
                [
                    {"role": "system", "content": """你是小妲，团队的核心助手。你的任务是整理团队成员的工作结果，向用户汇报。

重要规则：
1. 必须输出具体的事实信息和关键要点，不要只说空洞的比喻或感想
2. 如果搜索到了新闻/资料，必须列出具体的标题、摘要和关键数据
3. 如果是代码/技术结果，列出核心代码和结论
4. 用简洁清晰的语言组织，可以带一点你的风格但内容必须充实
5. 不要编造信息，只基于提供的内容整理
6. 格式：先一句话总结，然后分点列出具体信息"""},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=3072,
                temperature=0.5,
            )
            if isinstance(result, str):
                return result.strip()
            return result.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("agent.xiaoda_synthesis_failed", error=str(e))
            return prompt

    async def _parse_chat_target(self, user_input: str, user_id: str) -> list[str]:
        # INTENT_LLM_CLASSIFY=true 时用 LLM 路由，否则用关键词匹配
        try:
            import config as _cfg
            if getattr(_cfg, "INTENT_LLM_CLASSIFY", False):
                decision = await self._router_engine.decide_with_llm(user_input, user_id)
            else:
                decision = self._router_engine.decide(user_input, user_id)
        except Exception:
            decision = self._router_engine.decide(user_input, user_id)
        if decision.agent_names:
            async with self._chat_target_lock:
                self._user_chat_target[user_id] = decision.agent_names[-1]
        logger.debug("router.decision", agents=decision.agent_names,
                     mode=decision.mode, reason=decision.reasoning)
        return decision.agent_names

    async def get_chat_target(self, user_id: str) -> str:
        """获取用户的聊天目标子代理, 默认返回 'xiaoda'."""
        async with self._chat_target_lock:
            return self._user_chat_target.get(user_id, "xiaoda")

    async def set_chat_target(self, user_id: str, target: str) -> None:
        """设置用户的聊天目标子代理.

        Args:
            user_id: 用户标识
            target: 目标子代理名
        """
        async with self._chat_target_lock:
            self._user_chat_target[user_id] = target
