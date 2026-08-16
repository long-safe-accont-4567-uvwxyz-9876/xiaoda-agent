"""VerificationMixin —— Phase 2 拆分自 message_processor.py。

包含 Harness 验收循环（_run_verification_loop）、验收解析与调用
（_parse_verification_result / _call_and_parse_verification_llm）、验收收尾
（_finalize_verification_reply）以及续写/无工具收尾
（_retry_continuation / _finalize_reply_without_tools）。

模块级 ContextVar _system_context_var（P0-2 system_context 请求级隔离）定义也迁至
本模块，message_processor.py re-export 保持外部 import 兼容。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared / agent_core.mixins.greeting
及 config/core 叶子模块，不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar as _ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

from agent_core._shared import (
    DEGRADED_REPLY,
    _stream_finish_reason_var,
    get_empty_reply_for_finish_reason,
)
from agent_core.mixins.greeting import _force_close_incomplete_reply
from utils.llm_cleanup import merge_continuation
from utils.text_utils import (
    ends_with_valid_ending,
    has_dsml_tool_calls,
    is_reply_likely_complete,
    parse_dsml_tool_calls,
)

if TYPE_CHECKING:
    from agent_core._shared import RequestContext

# P0-2 修复：_system_context 改为 ContextVar，避免单例 AgentCore 并发覆写。
# 主动问候(nudge_engine)与用户消息并发时，实例属性 self._system_context 互相覆写，
# 导致场景提示串台。ContextVar 在 asyncio.Task 级别隔离，每个请求读到自己设置的值。
# Phase 2 拆分：定义自 message_processor.py 逐字节迁至本模块。
_system_context_var: _ContextVar[str] = _ContextVar("_system_context", default="")


class VerificationMixin:
    """Harness 验收循环相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

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
            return await self._finalize_reply_without_tools(
                first_result, messages, trace,
                task_type=task_type, temperature=temperature, max_tokens=max_tokens,
                user_openid=user_openid, session_id=session_id,
            ), []

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
                    # P0 修复（上下文污染根因）：assistant-prefill 续写，不追加 user message，
                    # 元指令不进入 LLM 可见上下文；用副本避免污染 verification loop 共享的 messages。
                    # P0 修复（阻塞根因）：超时取 min(LLM_CALL_TIMEOUT, remaining)，绝不超出墙钟。
                    early_reply, _early_action, _early_retry_len = await self._retry_continuation(
                        early_reply, messages,
                        task_type=task_type, temperature=temperature, max_tokens=max_tokens,
                        user_openid=user_openid, session_id=session_id,
                        timeout=min(self.LLM_CALL_TIMEOUT, _early_remaining),
                        context="after_tools_retry", min_len=10,
                    )
                except Exception as e:
                    trace.warning("verification.incomplete_retry_failed_after_tools", error=str(e))
                    break
                if _early_action == "discarded":
                    # 重试重复 = LLM 认为回复已完成，视为完整不再 force_close
                    _early_considered_complete = True
                    break  # 重试重复
                if _early_action == "empty":
                    break  # 重试返回空或太短
                trace.info("verification.incomplete_retry_success_after_tools",
                           final_len=len(early_reply), retry=_early_retry,
                           merge_action=_early_action)
            # 最终兜底：仅当 for 循环未判定完整时才处理
            # P0 修复：不再盲目追加 "。" —— 使用 emoji 感知的结尾判定
            if not _early_considered_complete:
                early_reply, _early_fc_action = _force_close_incomplete_reply(early_reply)
                if _early_fc_action == "degraded":
                    trace.warning("verification.empty_after_leak_strip_degraded_after_tools")
                elif _early_fc_action == "force_closed":
                    trace.warning("verification.incomplete_force_closed_after_tools", final_len=len(early_reply))
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

    async def _retry_continuation(
        self,
        reply: str,
        messages: list[dict],
        *,
        task_type: str,
        temperature: float,
        max_tokens: int | None,
        user_openid: str,
        session_id: str,
        timeout: float,
        context: str,
        min_len: int = 5,
        assume_tail: bool = False,
    ) -> tuple[str, str, int]:
        """assistant-prefill 续写重试：追加 assistant 消息后让 LLM 从截断处续写。

        不追加 user message，避免"请继续"等元指令污染上下文。
        返回 (new_reply, action, retry_len)，action 取值：
        - merge_continuation 的动作（"discarded"/"replaced"/"spliced"/"appended"）
        - "empty"：续写为空或过短
        异常由调用方捕获，以便保留各自的错误日志。
        """
        _retry_messages = list(messages)
        _retry_messages.append({"role": "assistant", "content": reply})
        _retry_result = await asyncio.wait_for(
            self.router.route(
                task_type, _retry_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                user_openid=user_openid,
                session_id=session_id,
            ),
            timeout=timeout,
        )
        if isinstance(_retry_result, str):
            _retry_text = _retry_result
        else:
            _retry_text = getattr(_retry_result.choices[0].message, "content", "") or ""
        _retry_text = self._clean_reply(_retry_text)
        if not _retry_text or len(_retry_text) <= min_len:
            return reply, "empty", len(_retry_text)
        _merged, _action = merge_continuation(
            reply, _retry_text, context=context, assume_tail=assume_tail)
        return _merged, _action, len(_retry_text)

    async def _finalize_reply_without_tools(
        self,
        first_result: Any,
        messages: list[dict],
        trace: Any,
        *,
        task_type: str,
        temperature: float,
        max_tokens: int | None,
        user_openid: str,
        session_id: str,
    ) -> str:
        """首轮无 tool_calls 时的回复处理：空回复保护 + 完整性检测 + 截断重试 + 句号兜底。"""
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
                return get_empty_reply_for_finish_reason("length")
            elif _finish_reason == "content_filter":
                # content_filter：内容被安全过滤，直接返回专用提示
                trace.warning("verification.empty_first_reply_content_filter")
                return get_empty_reply_for_finish_reason("content_filter")
            elif _finish_reason == "tool_calls":
                # tool_calls：LLM 想调用工具但没生成文本，给个友好提示
                trace.warning("verification.empty_first_reply_tool_calls_only")
                return get_empty_reply_for_finish_reason("tool_calls")
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
                reply, _retry_action, _retry_len = await self._retry_continuation(
                    reply, messages,
                    task_type=task_type, temperature=temperature, max_tokens=max_tokens,
                    user_openid=user_openid, session_id=session_id,
                    timeout=15, context="verification_length_retry", assume_tail=True,
                )
            except Exception as e:
                logger.warning("verification.length_retry_failed",
                               error=str(e)[:200], finish_reason=_finish_reason)
            else:
                if _retry_action == "empty":
                    logger.warning("verification.length_retry_empty",
                                   finish_reason=_finish_reason)
                elif _retry_action == "discarded":
                    logger.warning("verification.length_retry_duplicate",
                                   retry_len=_retry_len)
                else:
                    logger.info("verification.length_retry_success",
                                original_len=len(_reply_rstripped),
                                final_len=len(reply), action=_retry_action)
                    _reply_considered_complete = True
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
                reply, _retry_action, _retry_len = await self._retry_continuation(
                    reply, messages,
                    task_type=task_type, temperature=temperature, max_tokens=max_tokens,
                    user_openid=user_openid, session_id=session_id,
                    timeout=15, context="verification_no_finish_retry", assume_tail=True,
                )
            except Exception as e:
                logger.warning("verification.no_finish_retry_failed",
                               error=str(e)[:200], finish_reason=_finish_reason)
            else:
                if _retry_action == "discarded":
                    # 重试重复 = LLM 认为回复已完成，视为完整
                    _reply_considered_complete = True
                elif _retry_action == "empty":
                    # 空续写 = LLM 无续写内容，确认回复已完成，标记完整避免 force_close 追加 "。"
                    logger.info("verification.no_finish_retry_empty_confirmed_complete",
                                finish_reason=_finish_reason, reply_len=len(reply),
                                note="empty_retry_means_llm_confirmed_no_continuation")
                    _reply_considered_complete = True
                else:
                    logger.info("verification.no_finish_retry_success",
                                original_len=len(_reply_rstripped),
                                final_len=len(reply), action=_retry_action)
                    _reply_considered_complete = True
        elif _finish_reason == "length":
            # length 截断但回复太短无法重试
            logger.warning("verification.length_too_short_to_retry",
                           reply_len=len(reply), finish_reason=_finish_reason)
        # 最终兜底：仅当未判定完整时才处理
        # P0 修复：不再盲目追加 "。" —— 这会产生 "💕。" 丑陋输出
        # 改为：仅在回复不以合法标记结尾且较长时追加 "。"（极端兜底）
        if not _reply_considered_complete:
            reply, _fc_action = _force_close_incomplete_reply(reply)
            if _fc_action == "degraded":
                logger.warning("verification.empty_after_leak_strip_degraded")
            elif _fc_action == "force_closed":
                logger.warning("verification.incomplete_force_closed", final_len=len(reply))

        return reply
