"""消息处理 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含主处理流程 _process_impl 及消息分类、语音意图识别、图片描述、
聊天目标路由等消息处理相关方法。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import tempfile
from typing import Any, TYPE_CHECKING


from utils.common import safe_int as _safe_int

from loguru import logger

from config import (MIMO_MODEL, AGENT_CONFIG, build_safe_system_prompt,
                    SIMPLE_TASK_KEYWORDS, PRO_TASK_KEYWORDS, TTS_ASYNC_MODE,
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
                              strip_dsml)
from utils.llm_cleanup import deduplicate_multi_reply

# 从 _shared 导入共享常量, 避免重复定义 (该模块极轻量, 无循环导入风险)
from agent_core._shared import DEGRADED_REPLY, is_degraded_reply


def _get_temperature(model_cfg: dict | None = None) -> float:
    """读取 temperature：优先 webui_overrides，回退 agent.json5 默认值。"""
    from config import get_temperature
    default = float(model_cfg.get("temperature", 0.7)) if model_cfg else 0.7
    return get_temperature(default=default)

if TYPE_CHECKING:
    from agent_core._shared import RequestContext
from agent_core._shared import ProcessResult


class MessageProcessorMixin:
    """消息处理相关方法的 Mixin，由 AgentCore 组合使用。"""

    # ── Harness 验收循环常量 ──────────────────────────────────
    MAX_VERIFICATION_TURNS = 8          # 最大循环轮次
    VERIFICATION_WALL_TIMEOUT = 50      # 墙钟超时（秒）
    MAX_CONSECUTIVE_TOOL_FAILURES = 3   # 连续工具失败上限
    LLM_CALL_TIMEOUT = 30               # 单次 LLM 调用超时

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

        # 解析首轮 LLM 输出（提取 tool_calls、assistant_content、reasoning）
        current_tool_calls, current_assistant_content, current_reasoning = \
            self._parse_verification_result(first_result, tools)

        # 如果首轮没有 tool_calls，检测回复完整性后返回
        if not current_tool_calls:
            if isinstance(first_result, str):
                reply = self._clean_reply(first_result)
            else:
                _raw_content = first_result.choices[0].message.content or ""
                reply = self._clean_reply(_raw_content)
                # 诊断：捕获清洗前原始内容，定位 empty_reply 根因
                if not reply or not reply.strip():
                    logger.warning("debug.empty_reply_raw_capture",
                                   raw_len=len(_raw_content),
                                   raw_head=_raw_content[:300],
                                   finish_reason=getattr(first_result.choices[0], "finish_reason", None),
                                   has_tool_calls=bool(getattr(first_result.choices[0].message, "tool_calls", None)))

            # 空回复保护：如果首轮就返回空内容，抛异常让上层 fallback 机制接管
            if not reply or not reply.strip():
                trace.warning("verification.empty_first_reply")
                raise RuntimeError("empty_reply: LLM 返回空内容，触发 fallback")

            # 截断兜底：循环重试直到回复完整或达到最大重试次数，确保用户永不看到截断
            # 检测两种情况：
            # 1. 短回复不以句末标点结尾（如"嗯……让我查一下记忆里 7 月16号 7:00-8:"，26字以冒号结尾）
            # 2. 长回复最后一行很短且不以句末标点结尾（如列表截断"...3"，max_tokens 截断）
            for _retry_idx in range(3):
                _reply_rstripped = reply.rstrip()
                _last_line = _reply_rstripped.split('\n')[-1] if _reply_rstripped else ""
                _ends_with_punct = any(_reply_rstripped.endswith(c) for c in "。！？～…）」】\n")
                if _ends_with_punct or (len(reply) >= 60 and len(_last_line) >= 10):
                    break  # 回复完整
                logger.warning("verification.incomplete_reply",
                               reply_len=len(reply), reply_tail=reply[-15:], retry=_retry_idx)
                try:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "请继续完成你的回复，不要重复已说的内容。"})
                    result2 = await asyncio.wait_for(self.router.route(
                        task_type, messages, temperature=temperature, max_tokens=max_tokens,
                        user_openid=user_openid, session_id=session_id,
                    ), timeout=60)
                    reply2 = result2 if isinstance(result2, str) else (result2.choices[0].message.content or "")
                    reply2 = self._clean_reply(reply2)
                    if reply2 and len(reply2) > 5:
                        _reply_lower = reply.lower()
                        _reply2_lower = reply2.lower()
                        if _reply2_lower in _reply_lower:
                            logger.warning("verification.retry_duplicate",
                                           retry_len=len(reply2))
                            break  # 重试重复，不再继续
                        else:
                            _overlap = 0
                            _check_len = min(len(reply), len(reply2), 80)
                            for i in range(_check_len, 10, -1):
                                if reply[-i:].lower() == reply2[:i].lower():
                                    _overlap = i
                                    break
                            if _overlap > 10:
                                reply = reply + reply2[_overlap:]
                            else:
                                reply = reply + reply2
                            logger.info("verification.incomplete_retry_success",
                                        final_len=len(reply), retry=_retry_idx)
                    else:
                        break  # 重试返回空或太短
                except Exception as e:
                    logger.warning("verification.incomplete_retry_failed", error=str(e))
                    break
            # 最终兜底：达到最大重试次数仍不完整，用句末标点强制闭合，确保永不截断
            _final_rstripped = reply.rstrip()
            if not any(_final_rstripped.endswith(c) for c in "。！？～…）」】\n"):
                reply = _final_rstripped + "。"
                logger.warning("verification.incomplete_force_closed", final_len=len(reply))

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

            # 连续失败检查
            turn_failed = all(not r.success for r in turn_tool_results)
            if turn_failed:
                consecutive_failures += 1
                if consecutive_failures >= self.MAX_CONSECUTIVE_TOOL_FAILURES:
                    trace.warning("verification.max_failures", failures=consecutive_failures)
                    break
            else:
                consecutive_failures = 0

            # 再次调用 LLM 并解析结果（返回 early_reply 时表示验收通过）
            current_tool_calls, current_assistant_content, current_reasoning, early_reply = \
                await self._call_and_parse_verification_llm(
                    messages, tools, task_type, temperature, max_tokens,
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
        return await self._finalize_verification_reply(
            user_input, all_tool_results, last_tool_calls or [],
            current_assistant_content, trace, user_openid, session_id,
        )

    async def _process_impl(self, ctx: RequestContext, user_input: str, user_id: str,
                             source: str, user_openid: str, session_id: str,
                             status_callback: Any, image_data: list[dict] | None,
                             is_master: bool = True) -> ProcessResult:
        # 初始化 + 安全检查 + 上下文恢复
        trace, session_id, allowed, reason = await self._init_and_restore_context(
            ctx, user_input, user_id, source, status_callback, user_openid, session_id)
        if not allowed:
            trace.warning("agent.blocked", reason=reason)
            return ProcessResult(reply="")

        # XP 自动加成（fire-and-forget，不阻塞主流程；基于消息长度）
        try:
            from core.xp_system import get_xp_system
            _xp_uid = user_openid or user_id
            if _xp_uid:
                get_xp_system().add_chat_xp(_xp_uid, len(user_input))
        except Exception as _e:
            logger.warning("xp.auto_add_failed", error=str(_e))

        # 用户画像学习：记录交互统计 + 周期性 LLM 认知抽取（fire-and-forget）
        try:
            from core.user_profile_learner import get_user_profile_learner
            from core.xp_system import get_xp_system
            _learner = get_user_profile_learner()
            _xp_uid2 = user_openid or user_id
            if _xp_uid2:
                _is_deep = len(user_input) > 100
                _learner.record_interaction(_xp_uid2, len(user_input), is_deep=_is_deep)
                # 周期性触发 LLM 认知抽取（不阻塞，spawn 后台）
                if _learner.should_run_insight(_xp_uid2):
                    _xp_state = get_xp_system().get_state(_xp_uid2)
                    _lv = _xp_state.level.value if hasattr(_xp_state.level, 'value') else int(_xp_state.level)
                    _spawn(self._run_profile_insight(_xp_uid2, _lv))
        except Exception as _e:
            logger.warning("profile_learner.record_failed", error=str(_e))

        # slash 命令
        if self.slash_handler and self.slash_handler.is_slash_command(user_input):
            slash_reply = await self.slash_handler.handle(user_input, user_id)
            return ProcessResult(reply=slash_reply)

        chat_targets = await self._parse_chat_target(user_input, user_id)
        clean_input = ChatProcessor.clean_mention_from_input(user_input)

        voice_intent = self._detect_voice_intent(clean_input)
        force_voice = voice_intent and not self._voice_mode

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

        # 简单对话快速路径（跳过记忆检索，使用最小上下文）
        fast_result = await self._try_simple_chat_fast_path(
            ctx, user_input, clean_input, is_master, image_data, force_voice,
            session_id, user_openid, source, user_id, status_callback, trace)
        if fast_result is not None:
            return fast_result

        # 任务图路由
        graph_result = await self._try_task_graph_route(
            ctx, user_input, clean_input, chat_targets, force_voice, image_data,
            is_master, user_id, source, session_id, status_callback, trace)
        if graph_result is not None:
            return graph_result

        # 主处理路径：完整记忆检索 + LLM 调用 + 后处理
        return await self._run_main_process_path(
            ctx, user_input, clean_input, user_id, source, user_openid, session_id,
            status_callback, image_data, is_master, force_voice, chat_targets, trace)

    async def _init_and_restore_context(self, ctx: Any, user_input: Any, user_id: Any, source: Any,
                                         status_callback: Any, user_openid: Any, session_id: Any) -> tuple:
        """初始化 trace、发送状态提示、安全检查、恢复用户上下文。

        返回 (trace, session_id, allowed, reason)。
        """
        if self._tool_call_handler:
            self._tool_call_handler._tool_repair.clear_storm_window()

        _trace_id = f"{int(time.time()*1000)%1000000:06d}"
        trace = logger.bind(trace_id=_trace_id)
        trace.info("agent.process.start", source=source, user_id=user_id,
                    msg_preview=user_input[:80])

        allowed, reason = self.security.is_allowed(user_id)

        # 群聊 session 按用户隔离：不同用户使用不同 session_id
        # 保留原始 session_id 作为后缀，避免上层传入的值完全丢失
        if source == "qq_group" and user_openid:
            _orig_suffix = session_id.rsplit(":", 1)[-1] if session_id else ""
            session_id = f"qq_group:{user_openid}:{_orig_suffix}" if _orig_suffix else f"qq_group:{user_openid}"

        # 按当前用户恢复历史摘要（群聊多用户上下文隔离）
        _restore_id = user_openid or user_id
        if _restore_id:
            try:
                await self.context.switch_user_context(_restore_id)
            except Exception as e:
                logger.warning("agent.switch_user_context_failed", error=str(e))
        if _restore_id and self.db:
            try:
                await self.context.restore_from_db(self.db, user_id=_restore_id,
                                                    address_term=self.context.current_address_term)
            except Exception as e:
                logger.warning("agent.restore_failed", error=str(e))

        return trace, session_id, allowed, reason

    async def _try_simple_chat_fast_path(self, ctx: Any, user_input: Any, clean_input: Any, is_master: Any,
                                          image_data: Any, force_voice: Any, session_id: Any, user_openid: Any,
                                          source: Any, user_id: Any, status_callback: Any, trace: Any) -> Any:
        """简单对话快速路径：跳过记忆检索，使用最小上下文。返回 ProcessResult 或 None。"""
        if not (SIMPLE_CHAT_FASTPATH and self._is_simple_chat(clean_input)
                and not image_data and not ("[图片:" in user_input and "已保存到" in user_input)):
            return None

        trace.info("chat.fast_path", input_preview=clean_input[:50])
        emotion = detect_emotion(user_input)
        emotion_hint = build_emotion_hint(emotion)
        self.context.emotion_hint = emotion_hint
        ctx.last_user_emotion = emotion.get("primary", "")
        emotion_label = emotion.get("primary", "")
        self._update_mental_state_emotion(emotion)

        # 构建最小上下文
        messages = await self._build_fast_path_messages(user_input, is_master, emotion, emotion_hint, source)

        # LLM 调用
        reply = await self._call_fast_path_llm(messages, user_openid, session_id, is_master=is_master)

        # 空回复检测：返回 None 走主路径
        # 根因：agnes-2.0-flash 返回空 content 时，_call_fast_path_llm 可能返回空字符串
        # （route() 的空 content fallback 未覆盖 finish_reason=stop 的情况）
        # 修复：空 reply 一律走主路径，避免用户收到空回复
        if not reply or not reply.strip():
            logger.warning("chat.fast_path_empty_reply",
                           provider="agnes", reply_len=len(reply or ""))
            return None

        # 碎片检测：回复过短且未以句末标点结尾时，认为是 LLM 思考碎片泄漏
        # 根因：agnes-2.0-flash 对简短输入（如"？"）可能返回思考过程碎片
        # 如"我有点担心的是——你问「？"，这不是有效回复
        # 修复：返回 None 让上层走主路径（有 verification 循环，更可靠）
        if reply and len(reply) < 30:
            _end = reply[-5:] if len(reply) >= 5 else reply
            if not any(reply.endswith(c) for c in "。！？～…）」】\n"):
                logger.warning("chat.fast_path_fragment_detected",
                               reply_len=len(reply), reply_tail=_end,
                               reply_preview=reply[:80])
                return None  # 走主路径

        # 截断检测与重试：回复被截断时自动重试
        # 优化：只在真正截断时重试（finish_reason=length），避免误判
        # 阈值提高到500，覆盖大多数正常长回复；英文思维链泄露已由 strip_reasoning 清空
        if reply and len(reply) < 500:
            _end = reply[-5:] if len(reply) >= 5 else reply
            # 英文碎片检测：reply 末尾是连续 4+ 英文字母（如 "ious]"）说明是 LLM 推理泄漏
            # 此时重试只会重复内容，应直接走主路径（有 verification 循环，更可靠）
            _tail_english_leak = bool(re.search(r'[A-Za-z]{4,}[\]\)\}\}]?$', reply[-12:]))
            if _tail_english_leak:
                logger.warning("chat.fast_path_english_leak",
                               reply_len=len(reply), reply_tail=_end,
                               reply_preview=reply[:80])
                return None  # 走主路径
            if not any(reply.endswith(c) for c in "。！？～…）」】\n\"'"):
                logger.warning("chat.fast_path_truncated",
                               reply_len=len(reply), reply_tail=_end)
                # 重试一次：追加"请继续完成回复"提示
                try:
                    retry_messages = messages.copy()
                    retry_messages.append({"role": "assistant", "content": reply})
                    retry_messages.append({"role": "user", "content": "请继续完成你的回复，不要重复已说的内容。"})
                    retry_result = await asyncio.wait_for(self.router.route(
                        "chat", retry_messages, temperature=0.7, max_tokens=4096,
                        user_openid=user_openid, session_id=session_id,
                    ), timeout=30)
                    retry_reply = retry_result if isinstance(retry_result, str) else (retry_result.choices[0].message.content or "")
                    retry_reply = self._clean_reply(retry_reply)
                    if retry_reply and len(retry_reply) > 5:
                        # 去重检测：如果 retry_reply 与 reply 高度重叠（>60%），说明模型重复了内容
                        # 此时只追加非重叠部分，避免文本重复两遍
                        _reply_lower = reply.lower()
                        _retry_lower = retry_reply.lower()
                        # 检测完全重复：retry 完全包含在 reply 中（retry 是 reply 的子串）
                        # 修复：原 _retry_lower.endswith(_retry_lower[-50:]) 是 bug（自比较恒为 True）
                        _retry_in_reply = _retry_lower in _reply_lower
                        # reply 是 retry 的子串 → retry 是 reply 的扩展，应使用 retry 替换 reply
                        _reply_in_retry = _reply_lower in _retry_lower
                        if _retry_in_reply:
                            # retry 完全重复 reply 内容，丢弃 retry
                            logger.warning("chat.fast_path_retry_duplicate",
                                           retry_len=len(retry_reply))
                            # 不拼接，保留原 reply
                        elif _reply_in_retry:
                            # retry 是 reply 的扩展（reply 是 retry 的前缀），用 retry 替换 reply
                            reply = retry_reply
                            logger.info("chat.fast_path_retry_extended",
                                        final_len=len(reply))
                        else:
                            # 检测前缀重叠：retry_reply 开头部分是否与 reply 结尾部分相同
                            _overlap = 0
                            _check_len = min(len(reply), len(retry_reply), 100)
                            for i in range(_check_len, 10, -1):
                                if reply[-i:].lower() == retry_reply[:i].lower():
                                    _overlap = i
                                    break
                            if _overlap > 10:
                                # 去除重叠部分后拼接
                                reply = reply + retry_reply[_overlap:]
                            else:
                                reply = reply + retry_reply
                            logger.info("chat.fast_path_truncated_retry_success",
                                        final_len=len(reply))
                except Exception as e:
                    logger.warning("chat.fast_path_truncated_retry_failed", error=str(e))

        # 后处理
        result = await self._finalize_fast_path_reply(
            reply, user_input, is_master, user_id, source, emotion,
            emotion_label, ctx, user_openid, session_id, force_voice)
        trace.info("agent.fast_path.done", reply_preview=result.reply[:100],
                   reply_len=len(result.reply))
        return result

    async def _build_fast_path_messages(self, user_input: Any, is_master: Any,
                                          emotion: Any, emotion_hint: str,
                                          source: str = "") -> list:
        """构建快速路径的最小上下文消息列表：系统提示 + 动态提示 + Volatile 层 + 记忆 + 历史。"""
        # 构建最小上下文：系统提示 + 动态提示 + Volatile 层
        if is_master:
            system_prompt = build_scene_aware_prompt(user_input, self.context.current_address_term)
        else:
            system_prompt = build_safe_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]

        if is_master:
            _dynamic = self.context._build_dynamic_prompt()
            if _dynamic:
                messages.append({"role": "system", "content": _dynamic})

        _volatile = self.context._build_time_context()
        if emotion_hint:
            _addr = self.context.current_address_term if is_master else "你"
            _volatile += f"\n[感知到{_addr}的情绪：{emotion_hint}]"

        # 场景标识注入（让 LLM 感知私聊/群聊场景）
        if source:
            try:
                from agent_context import _build_scene_hint
                scene_hint = _build_scene_hint(source)
                if scene_hint:
                    _volatile += f"\n{scene_hint}"
            except Exception as e:
                logger.debug("message_processor.scene_hint_failed", error=str(e))

        # 历史对话（非主人不加载，防止看到主人聊天内容）
        if is_master:
            # 历史中可能混有子代理回复（通过 agent 元数据标记）
            # 用 XML 标签包裹子代理回复，让 LLM 知道这是其他 agent 说的，但不会模仿前缀
            # 根本修复：替代旧的 [小可] 文本前缀（LLM 会模仿），改用结构化标记
            _agent_display_cache = {}
            try:
                from config import get_agent_display_name as _gdn
                for _n in ('xiaoda', 'xiaoli', 'xiaolang', 'xiaolian', 'xiaoke'):
                    _agent_display_cache[_n] = _gdn(_n)
            except Exception:
                _agent_display_cache = {'xiaoda': '小妲', 'xiaoli': '小莉', 'xiaolang': '小狼',
                                        'xiaolian': '小涟', 'xiaoke': '小可'}
            for msg in self.context.get_last_n(10):
                _content = str(msg.get("content", "")) if msg.get("content") is not None else ""
                _msg_agent = msg.get("agent")
                # 子代理回复用 XML 标签包裹，LLM 不会模仿这种格式
                if _msg_agent and _msg_agent != "xiaoda":
                    _display = _agent_display_cache.get(_msg_agent, _msg_agent)
                    _content = f"<previous_agent_reply agent=\"{_msg_agent}\" name=\"{_display}\">{_content}</previous_agent_reply>"
                m = {"role": msg["role"], "content": _content}
                messages.append(m)

        # 轻量 FTS + 安抚记忆检索（放在历史之后，靠近用户消息，提高关注度）
        messages = await self._fast_path_inject_memories(
            messages, user_input, is_master, emotion)

        messages.append({"role": "system", "content": _volatile})
        messages.append({"role": "user", "content": user_input})
        return messages

    async def _call_fast_path_llm(self, messages: list, user_openid: Any,
                                    session_id: Any, is_master: bool = True) -> str:
        """快速路径 LLM 调用，返回回复文本（失败时返回降级回复）。

        支持 tool call：如果模型决定使用工具，解析并执行，返回执行结果。
        """
        _model_cfg = AGENT_CONFIG.get("model", {})
        reply = ""
        try:
            # 获取可用工具列表
            _tools_list = to_openai_tools()
            tools = _tools_list if _tools_list else None
            # 非主人：白名单过滤
            if not is_master and tools:
                tools = [t for t in tools if t.get("function", {}).get("name") in self.ALLOWED_NON_MASTER_TOOLS]
                if not tools:
                    tools = None

            result = await asyncio.wait_for(self.router.route(
                "chat", messages,
                temperature=_get_temperature(_model_cfg),
                user_openid=user_openid, session_id=session_id,
                tools=tools,
            ), timeout=30)

            # 检测并处理 tool call
            if isinstance(result, str):
                # DSML 格式 tool call 检测
                if has_dsml_tool_calls(result) and tools:
                    dsml_calls = parse_dsml_tool_calls(result, self.tool_repair._allowed_tools)
                    if dsml_calls:
                        # 执行工具调用
                        tool_results, all_failed = await self._execute_fast_path_tools(dsml_calls, user_openid=user_openid, session_id=session_id)
                        if tool_results and not all_failed:
                            # 将工具结果追加到消息列表，再次调用 LLM
                            messages.append({"role": "assistant", "content": result})
                            messages.append({"role": "user", "content": f"工具执行结果：\n{tool_results}\n\n请根据工具结果回复用户。"})
                            # 递归调用，但不再传递 tools（避免无限循环）
                            result2 = await asyncio.wait_for(self.router.route(
                                "chat", messages,
                                temperature=_get_temperature(_model_cfg),
                                user_openid=user_openid, session_id=session_id,
                            ), timeout=30)
                            if isinstance(result2, str):
                                reply = self._clean_reply(result2)
                            else:
                                reply = self._clean_reply(result2.choices[0].message.content or "")
                        elif all_failed:
                            # 工具全部失败，返回错误提示
                            reply = "抱歉，工具调用失败了，请稍后再试。"
                            logger.warning("fast_path.all_tools_failed", tool_results=tool_results)
                        else:
                            reply = self._clean_reply(result)
                    else:
                        reply = self._clean_reply(result)
                else:
                    reply = self._clean_reply(result)
            else:
                # OpenAI 格式 tool call 检测
                msg = result.choices[0].message
                if msg.tool_calls and tools:
                    tool_calls = [
                        {"id": str(tc.id), "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": str(tc.function.arguments) if tc.function.arguments else "{}"}}
                        for tc in msg.tool_calls
                    ]
                    # 执行工具调用
                    tool_results, all_failed = await self._execute_fast_path_tools(tool_calls, user_openid=user_openid, session_id=session_id)
                    if tool_results and not all_failed:
                        # 将工具结果追加到消息列表，再次调用 LLM
                        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": msg.tool_calls})
                        messages.append({"role": "user", "content": f"工具执行结果：\n{tool_results}\n\n请根据工具结果回复用户。"})
                        # 递归调用，但不再传递 tools（避免无限循环）
                        result2 = await self.router.route(
                            "chat", messages,
                            temperature=_get_temperature(_model_cfg),
                            user_openid=user_openid, session_id=session_id,
                        )
                        if isinstance(result2, str):
                            reply = self._clean_reply(result2)
                        else:
                            reply = self._clean_reply(result2.choices[0].message.content or "")
                    elif all_failed:
                        # 工具全部失败，返回错误提示
                        reply = "抱歉，工具调用失败了，请稍后再试。"
                        logger.warning("fast_path.all_tools_failed", tool_results=tool_results)
                    else:
                        reply = self._clean_reply(msg.content or "")
                else:
                    reply = self._clean_reply(msg.content or "")
        except Exception as e:
            logger.warning("agent.fast_path_failed", error=str(e))
            reply = DEGRADED_REPLY

        # 截断兜底：循环重试直到回复完整或达到最大重试次数，确保用户永不看到截断
        if reply and reply.strip():
            for _fp_retry in range(3):
                _fp_rstripped = reply.rstrip()
                _fp_last_line = _fp_rstripped.split('\n')[-1] if _fp_rstripped else ""
                _fp_ends_punct = any(_fp_rstripped.endswith(c) for c in "。！？～…）」】\n")
                if _fp_ends_punct or (len(reply) >= 60 and len(_fp_last_line) >= 10):
                    break  # 回复完整
                logger.warning("fast_path.incomplete_reply",
                               reply_len=len(reply), reply_tail=reply[-15:], retry=_fp_retry)
                try:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "请继续完成你的回复，不要重复已说的内容。"})
                    _fp_result2 = await asyncio.wait_for(self.router.route(
                        "chat", messages,
                        temperature=_get_temperature(_model_cfg),
                        user_openid=user_openid, session_id=session_id,
                    ), timeout=30)
                    _fp_reply2 = _fp_result2 if isinstance(_fp_result2, str) else (_fp_result2.choices[0].message.content or "")
                    _fp_reply2 = self._clean_reply(_fp_reply2)
                    if _fp_reply2 and len(_fp_reply2) > 5:
                        _fp_lower1 = reply.lower()
                        _fp_lower2 = _fp_reply2.lower()
                        if _fp_lower2 not in _fp_lower1:
                            reply = reply + _fp_reply2
                            logger.info("fast_path.incomplete_retry_success",
                                        final_len=len(reply), retry=_fp_retry)
                        else:
                            break  # 重试重复，不再继续
                    else:
                        break  # 重试返回空或太短
                except Exception as _fp_e:
                    logger.warning("fast_path.incomplete_retry_failed", error=str(_fp_e))
                    break
            # 最终兜底：达到最大重试次数仍不完整，用句末标点强制闭合，确保永不截断
            _fp_final = reply.rstrip()
            if not any(_fp_final.endswith(c) for c in "。！？～…）」】\n"):
                reply = _fp_final + "。"
                logger.warning("fast_path.incomplete_force_closed", final_len=len(reply))
        return reply

    async def _execute_fast_path_tools(self, tool_calls: list[dict],
                                        user_openid: str = "", session_id: str = "") -> tuple[str, bool]:
        """执行 fast path 中的工具调用。

        Returns:
            (result_str, all_failed): 结果摘要字符串 + 是否全部失败
        """
        if not tool_calls or not self._tool_call_handler:
            return "", False

        results = []
        failed_count = 0
        for tc in tool_calls:
            try:
                func_name = tc.get("function", {}).get("name", "")
                func_args = tc.get("function", {}).get("arguments", "{}")

                # 解析参数
                if isinstance(func_args, str):
                    try:
                        args_dict = json.loads(func_args)
                    except json.JSONDecodeError:
                        args_dict = {}
                else:
                    args_dict = func_args

                # 执行工具（user_id 用于工具内部用户隔离，session_id 仅供审计日志）
                result = await self._tool_call_handler._tool_executor.execute(
                    func_name, args_dict, user_id=user_openid
                )

                # 格式化结果
                if isinstance(result, dict):
                    result_str = json.dumps(result, ensure_ascii=False)
                else:
                    result_str = str(result)

                results.append(f"{func_name}: {result_str[:500]}")
            except Exception as e:
                logger.warning(f"fast_path.tool_execute_failed tool={tc.get('function', {}).get('name')} error={str(e)}")
                results.append(f"{tc.get('function', {}).get('name', 'unknown')}: 执行失败 - {str(e)}")
                failed_count += 1

        all_failed = failed_count == len(tool_calls)
        return "\n".join(results) if results else "", all_failed

    async def _finalize_fast_path_reply(self, reply: str, user_input: Any, is_master: Any,
                                          user_id: Any, source: Any, emotion: Any,
                                          emotion_label: str, ctx: Any, user_openid: Any,
                                          session_id: Any, force_voice: Any) -> ProcessResult:
        """快速路径后处理：隐私扫描、人格校验、上下文记录、情绪标签、语音构建。返回 ProcessResult。"""
        # 非主人输出侧隐私扫描
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
            await self.context.add_message("user", user_input)
            # 降级/错误回复跳过记忆写入，但保留 history 一致性（避免 user 消息无 assistant 回复）
            if is_degraded_reply(reply):
                logger.info("agent.skip_memory_degraded_reply", source=source, reply_preview=reply[:60])
                # 仍写入 history 保持对话连续性
                await self.context.add_message("assistant", reply[:200])
            else:
                # strip emotion tags before storing to memory
                _clean_for_memory = self.sticker_manager.strip_emotion_tag(reply)
                await self.context.add_message("assistant", _clean_for_memory)
                self._bg_task_manager.run_background_tasks(
                    user_input, _clean_for_memory, user_id, source, emotion, [], session_id=session_id)
        try:
            _spawn(self.router.flush_costs())
        except Exception as e:
            logger.error("费用统计刷新失败: {}", str(e))

        # 情绪标签
        if is_unified():
            reply, ensured_emotion = ensure_emotion_tag(reply)
            if ensured_emotion.value != emotion_label:
                emotion_label = ensured_emotion.value

        clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
        # 清理模型输出的推理/思考内容（Agnes 等模型会输出 [emotion thinking] 等标签）
        clean_reply = strip_dsml(clean_reply)
        clean_reply = strip_reasoning(clean_reply)
        # 清除日志时间戳泄露：LLM 从 conversation_logs 照搬 [HH:MM] 标记到回复里
        from utils.llm_cleanup import strip_log_timestamps
        clean_reply = strip_log_timestamps(clean_reply, context="fast_path")
        clean_reply = humanize(clean_reply, style="xiaoda")
        clean_reply = deduplicate_multi_reply(clean_reply, context="fast_path")
        # 名称替换：确保 LLM 输出中的旧名被替换为显示名
        try:
            from config import apply_agent_name_replacements
            clean_reply = apply_agent_name_replacements(clean_reply)
        except Exception:
            logger.debug("apply_agent_name_replacements failed", exc_info=True)

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
            get_emotion_state().update(emotion_label, _intensity_map.get(emotion_label, 0.5))
        except Exception as e:
            logger.debug("emotion_state.update_failed", error=str(e))

        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
                             audio_path=audio_path, tts_pending=tts_pending, tts_text=tts_text)

    async def _fast_path_inject_memories(self, messages: Any, user_input: Any, is_master: Any, emotion: Any) -> Any:
        """快速路径：注入 FTS 记忆和安抚记忆到 messages。"""
        # 降级检查: L2+ 关闭记忆检索, 直接返回不注入
        if not get_degradation_strategy().is_feature_available("memory_search"):
            return messages
        _is_greeting = bool(re.match(
            r'^(早[上安]?|中午|下午|晚上|晚安|你好|哈喽|hi|hello|hey)[好呀～~！!。.\s]*$',
            user_input.strip(), re.IGNORECASE)) and not any(
            kw in user_input for kw in ("记得", "上次", "以前", "说过", "告诉", "回忆"))

        if is_master and not _is_greeting and self.memory and getattr(self.memory, 'memory', None):
            try:
                _fts_mems = await self.memory.memory.search_memories_fts(user_input, limit=10)
                if _fts_mems:
                    _mem_lines = []
                    for m in _fts_mems:
                        _s = m.get("summary", "")
                        if not _s:
                            continue
                        if re.search(r'(晚上好|早上好|中午好|下午好|晚安|早安)', _s):
                            continue
                        _ts = m.get("timestamp", 0)
                        if _ts:
                            try:
                                _d = time.strftime("%m-%d %H:%M", time.localtime(float(_ts)))
                                _mem_lines.append(f"[{_d}] {_s[:300]}")
                            except (ValueError, TypeError, OSError):
                                _mem_lines.append(_s[:300])
                        else:
                            _mem_lines.append(_s[:300])
                    if _mem_lines:
                        messages.append({
                            "role": "system",
                            "content": "[相关长期记忆]\n" + "\n---\n".join(_mem_lines)
                        })
            except Exception as e:
                logger.debug(f"fast_path.fts_failed: {e}")

        # 主动检索 C：情绪触发（轻量版，仅强负面情绪时检索安抚记忆）
        if is_master and self.memory and emotion.get("valence") == "negative" \
                and float(emotion.get("intensity", 0.0)) >= 0.5:
            try:
                _comfort = await self.memory.retrieve_comfort_memories(limit=1)
                if _comfort:
                    _c_lines = []
                    for m in _comfort:
                        _s = m.get("summary", "")
                        if _s:
                            _c_lines.append(_s[:300])
                    if _c_lines:
                        _addr = self.context.current_address_term or "你"
                        messages.append({
                            "role": "system",
                            "content": f"[曾经让{_addr}开心的回忆（温柔陪伴时可以提起）]\n" + "\n".join(_c_lines)
                        })
            except Exception as e:
                logger.debug(f"fast_path.comfort_failed: {e}")
        return messages

    async def _try_task_graph_route(self, ctx: Any, user_input: Any, clean_input: Any, chat_targets: Any,
                                     force_voice: Any, image_data: Any, is_master: Any, user_id: Any, source: Any,
                                     session_id: Any, status_callback: Any, trace: Any) -> Any:
        """任务图路由路径。返回 ProcessResult 或 None（None 表示继续主路径）。"""
        if not ("xiaoda" in chat_targets and self._task_graph
                and not self._is_manual_target(user_input, user_id)
                and not self._is_simple_task(clean_input)
                and not force_voice and not image_data
                and not ("[图片:" in user_input and "已保存到" in user_input)):
            return None

        try:
            from task_orchestrator import run_task_graph  # 冷启动优化: 延迟导入
            graph_result = await run_task_graph(
                graph=self._task_graph,
                user_input=clean_input,
                user_id=user_id,
                session_id=session_id,
                status_callback=status_callback,
                agent_configs=self._agent_route_configs,
                dispatcher=self.dispatcher,
            )
            if graph_result.final_output:
                emotion = detect_emotion(clean_input)
                ctx.last_user_emotion = emotion.get("primary", "")
                # 仅主人群聊消息（及非群聊场景）记入记忆
                _should_remember = is_master or source != "qq_group"
                if _should_remember:
                    # 关键：写入对话历史，否则下一轮上下文丢失
                    await self.context.add_message("user", clean_input)
                    # 降级/错误回复不写入对话历史和记忆库，避免污染后续检索
                    if is_degraded_reply(graph_result.final_output):
                        logger.info("agent.skip_memory_degraded_reply",
                                    source=source, reply_preview=graph_result.final_output[:60])
                    else:
                        await self.context.add_message("assistant", graph_result.final_output)
                        self._bg_task_manager.run_background_tasks(
                            clean_input, graph_result.final_output, user_id, source, emotion, [],
                            session_id=session_id,
                        )
                emotion_label = emotion.get("primary", "")
                clean_reply = self._finalize_reply(graph_result.final_output, style="xiaoda")
                sticker_path = None
                audio_path = None
                tts_pending = False
                tts_text = ""
                should_generate_voice = self._voice_mode or force_voice
                if should_generate_voice and len(clean_reply) > 2:
                    if TTS_ASYNC_MODE:
                        tts_pending = True
                        tts_text = self._clean_reply(clean_reply)
                    else:
                        try:
                            target_agent = self.dispatcher.get_agent(graph_result.route_target)
                            if target_agent:
                                audio_path = await target_agent.synthesize(
                                    self._clean_reply(clean_reply), emotion=emotion_label)
                        except Exception as e:
                            logger.warning("agent.routed_tts_failed", error=str(e))
                if audio_path:
                    clean_reply = clean_reply + "\n\n🎙️ 语音消息已发送～"
                return ProcessResult(reply=clean_reply, emotion=emotion_label,
                                     sticker_path=sticker_path, audio_path=audio_path,
                                     tts_pending=tts_pending, tts_text=tts_text)
        except Exception as e:
            logger.warning("agent.task_graph_failed",
                           error_type=type(e).__name__, error=str(e))
            # 区分根因：超时/模型错误/工具错误 等，给用户更有意义的提示
            if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                _hint = "任务处理超时了，请稍后再试～"
            elif "content_filter" in str(e):
                _hint = "这个问题暂时无法回答，换个方式试试？"
            else:
                _hint = DEGRADED_REPLY
            return ProcessResult(reply=_hint)
        return None

    async def _run_main_process_path(self, ctx: Any, user_input: Any, clean_input: Any, user_id: Any, source: Any,
                                      user_openid: Any, session_id: Any, status_callback: Any, image_data: Any,
                                      is_master: Any, force_voice: Any, chat_targets: Any, trace: Any) -> Any:
        """主处理路径：完整记忆检索 + LLM 调用 + 后处理。"""
        # 记忆检索阶段
        emotion, emotion_label = await self._setup_main_emotion_and_memory(
            user_input, clean_input, chat_targets, is_master, ctx)

        # 消息构建阶段
        messages, _pre_picked_sticker, tools = await self._build_main_messages(
            user_input, is_master, image_data, clean_input, emotion, user_id, source)

        # 任务类型解析与熔断器检查
        early_result, task_type, _cb_max_tokens, circuit_state, _model_cfg = \
            self._resolve_task_and_circuit(user_input, tools, messages, trace, source=source)
        if early_result is not None:
            return early_result

        # 主 LLM 调用 + 验收循环
        is_owner = self.security.is_owner(user_id)
        reply, tool_results = await self._call_main_llm_with_verification(
            messages, tools, task_type, _model_cfg, _cb_max_tokens, circuit_state,
            status_callback, user_openid, session_id, trace, ctx, user_input, is_owner)

        # 后处理阶段（含媒体提取与隐私扫描）
        return await self._finalize_main_reply(
            reply, tool_results, user_input, user_id, source, emotion,
            emotion_label, ctx, user_openid, is_master, _pre_picked_sticker, force_voice, trace,
            session_id)

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
        emotion_hint = build_emotion_hint(emotion)
        self.context.emotion_hint = emotion_hint
        ctx.last_user_emotion = emotion.get("primary", "")
        self._update_mental_state_emotion(emotion)

        # 记忆检索与 notebook 上下文加载并行化
        memories = await self._retrieve_main_memories(user_input, is_master, emotion)
        self.context.memory_retrieval = memories if memories else None

        emotion_label = emotion.get("primary", "")
        return emotion, emotion_label

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
            messages = self.context.build_messages(effective_input, source=source or "")

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
                await self.context.add_message("user", user_input)
                # 降级/错误回复跳过记忆写入，但保留 history 一致性
                if is_degraded_reply(reply):
                    logger.info("agent.skip_memory_degraded_reply", source=source, reply_preview=reply[:60])
                    await self.context.add_message("assistant", reply[:200])
                else:
                    rc = self.router.pop_reasoning_content()
                    # strip emotion tags before storing to memory
                    _clean_for_memory = self.sticker_manager.strip_emotion_tag(reply)
                    await self.context.add_message("assistant", _clean_for_memory, reasoning_content=rc)
                    self._bg_task_manager.run_background_tasks(
                        user_input, _clean_for_memory, user_id, source, emotion, tool_results, session_id=session_id)
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
            clean_reply = strip_dsml(clean_reply)
            clean_reply = strip_reasoning(clean_reply)
            clean_reply = humanize(clean_reply, style="xiaoda")
            clean_reply = deduplicate_multi_reply(clean_reply, context="main_path")
            # 名称替换：确保 LLM 输出中的旧名被替换为显示名
            try:
                from config import apply_agent_name_replacements
                clean_reply = apply_agent_name_replacements(clean_reply)
            except Exception:
                logger.debug("apply_agent_name_replacements failed", exc_info=True)

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
            get_emotion_state().update(emotion_label, _intensity_map.get(emotion_label, 0.5))
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
        async def _retrieve_memories() -> Any:
            # 降级检查: L2+ 关闭记忆检索, 跳过以减少负载
            if not get_degradation_strategy().is_feature_available("memory_search"):
                return None
            if self.memory and is_master:
                self.memory.signal_new_message()
                try:
                    _k = self.memory._suggest_k(user_input, default_k=8)
                    results = await self.memory.retrieve_memories(user_input, k=_k)
                except Exception as e:
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
            try:
                await self._load_notebook_context()
            except Exception as e:
                logger.warning("notebook.load_failed", error=str(e))

        async def _retrieve_constraint_lessons() -> list[dict]:
            """检索 RAG 层经验教训（FTS 关键词匹配，零成本）。"""
            try:
                from core.constraint_injector import search_constraint_lessons
                lessons = search_constraint_lessons(user_input, top_k=3)
                if lessons:
                    return [{"summary": f"[经验] {line}", "timestamp": 0,
                             "source": "constraint_rag"}
                            for line in lessons]
            except Exception as e:
                logger.debug("constraint.rag_search_failed", error=str(e))
            return []

        # 记忆检索 + notebook + 约束经验并行加载
        # 不允许跳过记忆检索 —— 各环节内部已有独立超时与 fallback
        # 但整体加 20s 兜底超时：任一环节挂起不允许阻塞整个请求（保证无超时）
        try:
            memories, _, _lessons = await asyncio.wait_for(
                asyncio.gather(
                    _retrieve_memories(), _load_notebook(), _retrieve_constraint_lessons()),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            logger.warning("memory.retrieve_global_timeout",
                           hint="记忆检索整体超时，跳过记忆继续生成回复（保证请求成功）")
            memories = None

        # 注：constraint_lessons 不再追加到 memories
        # 根因：jieba 子串匹配粗糙，单关键词命中即入选，极易注入无关经验，
        # 且以 "[经验] xxx" 格式混入 memory_retrieval，污染记忆检索结果。
        # 经验教训应通过独立通道（如 volatile 层）注入，不混入"相关记忆"。

        # ContextNest A2: 审计本次响应消费了哪些记忆版本 (point-in-time 重建支持)
        if memories and hasattr(self.memory, "audit_retrieval"):
            try:
                from memory.context_governance import ContextGovernance
                _response_id = ContextGovernance.new_response_id()
                _audited = await self.memory.audit_retrieval(_response_id, memories)
                if _audited:
                    logger.debug("memory.audited",
                                 response_id=_response_id, count=_audited)
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
        has_image = image_data or ("[图片:" in user_input and "已保存到" in user_input)
        if has_image and tools:
            tools = None

        # 简单任务时过滤系统级工具
        tools = ChatProcessor.filter_tools_for_simple_task(tools, clean_input, self._is_simple_task)
        # 表情包意图时仅保留 delegate_task
        if _sticker_intent and tools:
            tools = [t for t in tools if t.get("function", {}).get("name") == "delegate_task"]
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
        base_task = "chat_pro" if should_escalate else "chat"
        task_type = self.router.resolve_task_type(base_task)
        if should_escalate:
            trace.info("chat.escalated_to_pro", reason=reason)

        _model_cfg = AGENT_CONFIG.get("model", {})
        circuit_state = self._circuit_breaker.check(self._cognitive_state)
        if circuit_state == CircuitState.RED:
            logger.warning("agent.circuit_breaker_red")
            return ProcessResult(reply="系统需要休息一下，请稍后再试吧～"), \
                task_type, None, circuit_state, _model_cfg
        if circuit_state == CircuitState.HALF_OPEN:
            logger.info("agent.circuit_breaker_half_open_probe")

        _cb_max_tokens = None
        # Web UI 近似 Hermes 无限流式输出：使用 8192 tokens 上限（可配置）
        # QQ 通道保持 None → 走 ROUTE_TABLE 默认值（1500），避免超长回复被 QQ 平台截断
        _web_max_tokens = _safe_int(os.getenv("WEB_UI_MAX_TOKENS", "8192"), 8192)
        if source == "web":
            _cb_max_tokens = _web_max_tokens
        if circuit_state == CircuitState.YELLOW:
            messages.append({
                "role": "system",
                "content": "[系统警告] 当前认知状态不佳，请简化回复。"
            })
            _base_mt = _cb_max_tokens if _cb_max_tokens else _model_cfg.get("max_tokens", 4096)
            _cb_max_tokens = int(_base_mt * 0.8)
        return None, task_type, _cb_max_tokens, circuit_state, _model_cfg

    async def _call_main_llm_with_verification(self, messages: Any, tools: Any, task_type: Any, _model_cfg: Any,
                                                _cb_max_tokens: Any, circuit_state: Any, status_callback: Any,
                                                user_openid: Any, session_id: Any, trace: Any, ctx: Any, user_input: Any, is_owner: Any) -> tuple:
        """主 LLM 调用 + 验收循环 + 熔断器状态更新。返回 (reply, tool_results)。"""
        reply = ""
        tool_results = []
        try:
            if STREAM_TEXT_PUSH and status_callback and not tools:
                result = await self._stream_llm_response(
                    messages, status_callback=status_callback, task_type=task_type,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    user_openid=user_openid, session_id=session_id,
                )
            else:
                result = await asyncio.wait_for(self.router.route(
                    task_type, messages,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    user_openid=user_openid, session_id=session_id,
                ), timeout=120)

            # Harness 验收循环
            reply, tool_results = await self._run_verification_loop(
                result, messages, tools, trace,
                task_type=task_type,
                temperature=_get_temperature(_model_cfg),
                max_tokens=_cb_max_tokens,
                user_openid=user_openid, session_id=session_id,
                is_owner=is_owner, ctx=ctx, user_input=user_input,
            )
            if tool_results:
                ctx.handled_by_tool_call = True
            # 最终防线：如果 verification loop 返回空回复，触发 fallback
            if not reply or not reply.strip():
                logger.warning("agent.empty_reply_guard", tool_count=len(tool_results))
                raise RuntimeError("empty_reply_guard: verification loop 返回空回复")
            logger.info("agent.got_reply", length=len(reply), preview=reply[:80],
                        tool_count=len(tool_results))
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
                        "chat_flash", messages, temperature=0.7,
                        user_openid=user_openid, session_id=session_id,
                    )
                    reply = self._clean_reply(result) if isinstance(result, str) else DEGRADED_REPLY
                except Exception as e:
                    logger.debug(f"agent.flash_fallback: {e}")
                    reply = DEGRADED_REPLY
        return reply, tool_results

    async def _build_voice_result(self, clean_reply: Any, emotion_label: Any, force_voice: Any) -> tuple:
        """构建语音合成结果。返回 (audio_path, tts_pending, tts_text)。"""
        audio_path = None
        tts_pending = False
        tts_text = ""
        should_generate_voice = self._voice_mode or force_voice
        if (should_generate_voice and self.tts.available and len(clean_reply) > 2
                and get_degradation_strategy().is_feature_available("tts")):
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
            # 检测两种情况：
            # 1. 短回复且只是开场白（如"让我查一下"）
            # 2. 长回复最后一行很短且不以句末标点结尾（如列表截断"...3"，max_tokens 截断）
            for _early_retry in range(3):
                _early_rstripped = early_reply.rstrip()
                _early_last_line = _early_rstripped.split('\n')[-1] if _early_rstripped else ""
                _early_ends_punct = any(_early_rstripped.endswith(c) for c in "。！？～…）」】\n")
                _early_has_opening = any(kw in early_reply for kw in ["让我", "查一下", "看看", "查查", "找找"])
                _early_complete = _early_ends_punct or (
                    len(early_reply) >= 80 and len(_early_last_line) >= 10
                )
                if _early_complete:
                    break  # 回复完整
                # 只有短回复开场白 或 最后一行很短时才重试
                _need_retry = (
                    (len(early_reply) < 80 and _early_has_opening)
                    or len(_early_last_line) < 10
                )
                if not _need_retry:
                    break  # 不符合重试条件
                trace.warning("verification.incomplete_reply_after_tools",
                              reply_len=len(early_reply), reply_preview=early_reply[:50], retry=_early_retry)
                try:
                    messages.append({"role": "assistant", "content": early_reply})
                    messages.append({"role": "user", "content": "请继续给出具体内容，不要只说开场白。"})
                    retry_result = await asyncio.wait_for(
                        self.router.route(
                            task_type, messages, temperature=temperature, max_tokens=max_tokens,
                            user_openid=user_openid, session_id=session_id,
                        ),
                        timeout=self.LLM_CALL_TIMEOUT,
                    )
                    retry_reply = retry_result if isinstance(retry_result, str) else (retry_result.choices[0].message.content or "")
                    retry_reply = self._clean_reply(retry_reply)
                    if retry_reply and len(retry_reply) > 10:
                        _early_lower1 = early_reply.lower()
                        _early_lower2 = retry_reply.lower()
                        if _early_lower2 in _early_lower1:
                            break  # 重试重复
                        _early_overlap = 0
                        _early_check = min(len(early_reply), len(retry_reply), 80)
                        for i in range(_early_check, 10, -1):
                            if early_reply[-i:].lower() == retry_reply[:i].lower():
                                _early_overlap = i
                                break
                        if _early_overlap > 10:
                            early_reply = early_reply + retry_reply[_early_overlap:]
                        else:
                            early_reply = early_reply + retry_reply
                        trace.info("verification.incomplete_retry_success_after_tools",
                                   final_len=len(early_reply), retry=_early_retry)
                    else:
                        break  # 重试返回空或太短
                except Exception as e:
                    trace.warning("verification.incomplete_retry_failed_after_tools", error=str(e))
                    break
            # 最终兜底：达到最大重试次数仍不完整，用句末标点强制闭合
            _early_final = early_reply.rstrip()
            if not any(_early_final.endswith(c) for c in "。！？～…）」】\n"):
                early_reply = _early_final + "。"
                trace.warning("verification.incomplete_force_closed_after_tools", final_len=len(early_reply))
            return None, "", None, early_reply

        return current_tool_calls, current_assistant_content, current_reasoning, None

    async def _finalize_verification_reply(self, user_input: Any, all_tool_results: Any, last_tool_calls: Any,
                                            current_assistant_content: Any, trace: Any, user_openid: Any, session_id: Any) -> tuple:
        """验收循环结束后生成最终回复。"""
        trace.info("verification.summarize_fallback", tool_count=len(all_tool_results))
        if all_tool_results:
            final_reply = await self._tool_call_handler._summarize_results(
                user_input, all_tool_results, last_tool_calls,
                trace, user_openid=user_openid, session_id=session_id,
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
            if accumulated:
                logger.info("message_processor.stream_partial_return len={}", len(accumulated))
                return accumulated + "\n\n[⚠️ 内容生成中断，以上为已生成的部分]"
            return await self.router.route(task_type, messages, **kwargs)
        return "".join(full_response)

    def _should_escalate_to_pro(self, user_msg: str, tools: list | None) -> tuple[bool, str]:
        tool_keywords = PRO_TASK_KEYWORDS["tool"]
        if tools and any(kw in user_msg for kw in tool_keywords):
            return True, "tool_likely_query"

        negative = PRO_TASK_KEYWORDS["negative"]
        if any(kw in user_msg for kw in negative) and len(user_msg) > 30:
            return True, "deep_emotional_content"

        if len(user_msg) > 300:
            return True, "long_complex_message"

        return False, ""

    def _is_simple_task(self, user_input: str) -> bool:
        # 否定指令（告诉助手不要做某事）优先判断，属于简单对话，不应路由到子Agent
        negative_patterns = [
            r"(?:不|别|不要|不用|不需要|没必要)\s*(?:要|用|调用|查|检查|执行|运行|搜索|搜|找|看)",
            r"(?:不需要|不用|别)\s*(?:调用|使用)\s*(?:这个|那个|任何)?\s*(?:工具|功能)",
        ]
        if any(re.search(pat, user_input) for pat in negative_patterns):
            return True

        complex_keywords = SIMPLE_TASK_KEYWORDS["complex"]
        if any(kw in user_input for kw in complex_keywords):
            return False

        # 对话性消息关键词 — 这些是日常聊天，不是复杂任务
        chat_keywords = SIMPLE_TASK_KEYWORDS["chat"]
        if any(kw in user_input for kw in chat_keywords):
            return True

        cn_chars = sum(1 for c in user_input if '\u4e00' <= c <= '\u9fff')
        effective_len = cn_chars * 2 + len(user_input) - cn_chars
        if effective_len <= 20:
            return True
        simple_tool_patterns = ["天气", "气温", "时间", "几点", "日期", "星期", "翻译"]
        return bool(effective_len <= 25 and any(kw in user_input for kw in simple_tool_patterns))

    def _is_simple_chat(self, query: str) -> bool:
        """Task 9: 判断是否为简单闲聊，可走快速路径（跳过记忆检索）。

        判定规则（满足任一即返回 True）：
        1. 命中 SIMPLE_TASK_KEYWORDS["chat"] 中的闲聊关键词
        2. 有效长度（中文×2 + 其他×1）≤ 10
        但若命中 complex 关键词则返回 False，避免误判。
        """
        # 复杂任务关键词优先排除
        complex_keywords = SIMPLE_TASK_KEYWORDS["complex"]
        if any(kw in query for kw in complex_keywords):
            return False
        # 时间/日期查询排除：包含日期或时间表述的查询通常需要记忆检索
        import re
        if re.search(r'\d+[号日月年]|周[一二三四五六日末]|今[天日早晚]|昨[天日]|前天|上周|上个月', query):
            return False
        # 闲聊关键词命中
        chat_keywords = SIMPLE_TASK_KEYWORDS["chat"]
        if any(kw in query for kw in chat_keywords):
            return True
        # 有效长度 ≤ 10 视为简单闲聊
        cn_chars = sum(1 for c in query if '\u4e00' <= c <= '\u9fff')
        effective_len = cn_chars * 2 + len(query) - cn_chars
        if effective_len > 10:
            return False

        # 短追问场景保护：当前输入很短（如"？""然后呢""继续"），
        # 若上一轮用户消息是记忆/回忆/复杂查询，则不走 fast_path，
        # 否则会跳过记忆检索导致用户追问时无法触发 recall。
        if self._is_followup_after_memory_intent(query):
            return False
        return True

    def _is_followup_after_memory_intent(self, query: str) -> bool:
        """判断当前短输入是否是对上一轮记忆/复杂查询的追问。

        场景：用户问"回忆一下7月17日..."，模型回复不理想，用户发"？"追问。
        此时 fast_path 会把"？"当闲聊处理，跳过 recall，导致追问永远无法触发记忆检索。
        """
        # 短追问标记词：纯标点或极短追问
        import re
        _followup_markers = (
            "？", "?", "！", "!",
            "然后呢", "接着", "继续", "说下去", "然后",
            "呢", "嗯", "啊", "哦", "噢", "啥",
        )
        stripped = query.strip()
        # 仅当输入本身就是短追问标记时才检查上下文
        if stripped not in _followup_markers and not re.fullmatch(r'[？?！!\s]{1,3}', stripped):
            return False

        # 检查上下文中最近的用户消息是否含记忆/回忆/时间意图
        try:
            recent = self.context.get_last_n(6) or []
        except Exception:
            return False
        _memory_intent_keywords = (
            "回忆", "记得", "记忆", "recall", "上次", "昨天", "上周",
            "之前", "前天", "几点", "哪个时间", "那天",
        )
        # 倒序查找最近一条 user 消息（排除当前这条）
        for msg in reversed(recent):
            if msg.get("role") != "user":
                continue
            content = str(msg.get("content", ""))
            if any(kw in content for kw in _memory_intent_keywords):
                return True
            # 找到最近一条 user 消息即可，不含记忆意图就不再往前找
            return False
        return False

    def _detect_voice_intent(self, user_input: str) -> bool:
        voice_keywords = [
            "语音", "声音", "说话", "朗读", "念给我", "读给我",
            "用声音", "听你", "听听你", "发语音", "生成语音",
            "语音回复", "语音消息", "说给我听", "念出来",
            "tts", "voice",
        ]
        q = user_input.lower()
        return any(kw in q for kw in voice_keywords)

    def _update_mental_state_emotion(self, emotion: dict) -> None:
        """将检测到的用户情绪更新到 L/M/S 心理状态模型的 S 层.

        受 MENTAL_STATE_ENABLED 环境变量控制, 默认开启.
        任何异常都被吞掉, 不影响主消息处理流程.
        """
        try:
            from core.mental_state import get_mental_state_manager
            mgr = get_mental_state_manager()
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
                task_type="chat_flash",
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

    async def _describe_images(self, image_data: list[dict]) -> str:
        """使用 MiMo Vision API 识别图片内容"""
        try:
            if not self.router or not self.router._client:
                logger.warning("agent.vision_no_client")
                return ""

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

            response = await self.router._client.chat.completions.create(
                model=MIMO_MODEL,
                messages=[{"role": "user", "content": vision_parts}],
                max_tokens=1024,
            )
            description = response.choices[0].message.content.strip()
            logger.info("agent.image_described", length=len(description))
            return description
        except Exception as e:
            logger.warning("agent.image_describe_failed", error=str(e))
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