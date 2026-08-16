"""消息处理 Mixin —— 拆分自原 agent_core.py 的 AgentCore 类。

包含主处理流程 _process_impl 及消息分类、图片描述、聊天目标路由等
消息处理相关方法。

Phase 1 拆分：问候（GreetingMixin）与语音判定（VoiceMixin）逻辑已迁至
agent_core/mixins/，本模块 re-export 保持外部 import 兼容。
Phase 2 拆分：Harness 验收循环（VerificationMixin）逻辑已迁至
agent_core/mixins/verification.py，本模块 re-export 保持外部 import 兼容。
Phase 3 拆分：主处理路径（MainPathMixin）逻辑已迁至
agent_core/mixins/main_path.py，本模块经 MRO 组合使用。
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, NamedTuple

import openai as _openai_mod  # P0 Task 1.8：用于捕获 BadRequestError/APIError
from loguru import logger

from agent_core._shared import (
    ALLOWED_NON_MASTER_TOOLS as _ALLOWED_NON_MASTER_TOOLS,
)

# 从 _shared 导入共享常量, 避免重复定义 (该模块极轻量, 无循环导入风险)
from agent_core._shared import (
    _current_request_ctx,
    _stream_finish_reason_var,
)
from agent_core.mixins.greeting import (
    _SH_TZ as _SH_TZ,  # re-export（白盒测试依赖 mp_module._SH_TZ）
)

# ── Phase 1/2/3 拆分：问候/语音/验收循环/主处理路径逻辑迁至 agent_core/mixins/，此处 re-export 保持外部兼容 ──
# 叶子模块依赖约定：mixin 只依赖 agent_core._shared 及 config/core 叶子模块，
# 不得 import agent_core.message_processor（避免循环导入）。
# `as` 冗余别名是 ruff 认可的显式 re-export 标记（避免 F401）。
from agent_core.mixins.greeting import (
    GreetingMixin,
)
from agent_core.mixins.greeting import (
    _force_close_incomplete_reply as _force_close_incomplete_reply,
)
from agent_core.mixins.greeting import (
    _is_greeting_enabled as _is_greeting_enabled,  # re-export（外部兼容）
)
from agent_core.mixins.greeting import (
    _time_greeting_for_hour as _time_greeting_for_hour,  # re-export（外部兼容）
)
from agent_core.mixins.main_path import MainPathMixin
from agent_core.mixins.verification import (
    VerificationMixin,
)
from agent_core.mixins.verification import (
    _system_context_var as _system_context_var,  # re-export（白盒测试依赖 mp._system_context_var）
)
from agent_core.mixins.voice import (
    VoiceMixin,
)
from agent_core.mixins.voice import (
    _decide_tts_trigger as _decide_tts_trigger,  # re-export（sub_agent_manager 依赖）
)
from agent_core.mixins.voice import (
    _get_temperature as _get_temperature,
)
from agent_core.mixins.voice import (
    _should_auto_tts as _should_auto_tts,  # re-export（向后兼容别名）
)
from config import STREAM_TEXT_PUSH, get_agent_display_name
from core.background_tasks import _spawn
from core.chat_processor import ChatProcessor

if TYPE_CHECKING:
    from agent_core._shared import RequestContext
from agent_core._shared import ProcessResult


class InitRestoreResult(NamedTuple):
    """初始化 + 上下文恢复阶段的结果（原裸 4 元组改为命名结构）。"""
    trace: Any
    session_id: str
    allowed: bool
    reason: str


class MessageProcessorMixin(MainPathMixin, VerificationMixin, GreetingMixin, VoiceMixin):
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
    # VULN-27：唯一定义在 agent_core._shared（执行层门禁共用），此处仅引用
    ALLOWED_NON_MASTER_TOOLS: frozenset[str] = _ALLOWED_NON_MASTER_TOOLS

    def _parse_mode_markers(self, user_input: str) -> str:
        """解析前端模式标记（[Search:]/[Think:]/[Doc:]），剥离标记并注入模式提示。

        返回剥离标记后的 user_input。模式指令通过 system_context 注入（不入库）。
        """
        self._think_mode = False
        self._search_mode = False
        _mode_system_hint = ""
        if isinstance(user_input, str):
            _stripped_ui = user_input.strip()
            _m_search = re.match(r'^\[Search:\s*(.+?)\]\s*$', _stripped_ui)
            if _m_search:
                _sq = _m_search.group(1)
                self._search_mode = True
                user_input = _sq
                _mode_system_hint = "本次回复请优先使用 web_search 工具搜索最新信息后回答。"
            else:
                _m_think = re.match(r'^\[Think:\s*(.+?)\]\s*$', _stripped_ui)
                if _m_think:
                    self._think_mode = True
                    user_input = _m_think.group(1)
                    _mode_system_hint = "本次回复请进行更深入的思考，可以分步骤推理。"
            _m_doc = re.search(r'\n?\[Doc:\s*([^\]]+)\]\s*', user_input)
            if _m_doc:
                _doc_path = _m_doc.group(1).strip()
                user_input = user_input.replace(_m_doc.group(0), "").strip()
                _doc_hint = f"用户上传了文档：{_doc_path}。请使用 document_reader 工具读取该文档内容后回答用户的问题。"
                _mode_system_hint = (_mode_system_hint + "\n" + _doc_hint).strip() if _mode_system_hint else _doc_hint
                logger.info("agent.doc_marker_parsed", doc_path=_doc_path)
        if _mode_system_hint:
            self._system_context = (self._system_context + "\n" + _mode_system_hint).strip() if self._system_context else _mode_system_hint
            _system_context_var.set(self._system_context)
        return user_input

    async def _call_with_timeout(self, coro: Any, *, timeout: float,
                                 timeout_log: str, error_log: str,
                                 timeout_kwargs: dict | None = None) -> None:
        """带超时执行协程；超时/异常均降级跳过并记录日志，不抛异常。"""
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(timeout_log, timeout=timeout, **(timeout_kwargs or {}))
        except Exception as e:
            logger.warning(error_log, error=str(e))

    def _spawn_xp_and_profile(self, user_input: str, user_id: str, user_openid: str) -> None:
        """XP 加成 + 用户画像统计（fire-and-forget，不阻塞主消息流程）。"""
        try:
            from core.user_profile_learner import get_user_profile_learner
            from core.xp_system import get_xp_system
            _xp_uid = user_openid or user_id
            if _xp_uid:
                _xp = get_xp_system()
                _learner = get_user_profile_learner()
                _is_deep = len(user_input) > 100
                _spawn(asyncio.gather(
                    asyncio.to_thread(_xp.add_chat_xp, _xp_uid, len(user_input)),
                    asyncio.to_thread(
                        _learner.record_interaction, _xp_uid, len(user_input), is_deep=_is_deep),
                ), timeout=20)
                if _learner.should_run_insight(_xp_uid):
                    _xp_state = _xp.get_state(_xp_uid)
                    _lv = _xp_state.level.value if hasattr(_xp_state.level, 'value') else int(_xp_state.level)
                    _spawn(self._run_profile_insight(_xp_uid, _lv), timeout=45)
        except Exception as _e:
            logger.warning("xp.profile.record_failed", error=str(_e))

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
        user_input = self._parse_mode_markers(user_input)
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

        # XP 加成 + 用户画像统计（fire-and-forget）
        self._spawn_xp_and_profile(user_input, user_id, user_openid)

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
        force_voice = self._resolve_voice_force(clean_input)

        early_result = await self._maybe_dispatch_chat_target(
            chat_targets, clean_input, user_id, source, session_id, trace, force_voice, ctx)
        if early_result is not None:
            return early_result

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

    async def _maybe_dispatch_chat_target(
        self, chat_targets: list[str], clean_input: str, user_id: str, source: str,
        session_id: str, trace: Any, force_voice: bool, ctx: RequestContext,
    ) -> ProcessResult | None:
        """处理空输入确认与子 agent 路由；命中返回结果，否则返回 None 走主路径。"""
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
        return None

    async def _init_and_restore_context(self, ctx: Any, user_input: Any, user_id: Any, source: Any,
                                         status_callback: Any, user_openid: Any, session_id: Any) -> InitRestoreResult:
        """初始化 trace、发送状态提示、安全检查、恢复用户上下文。

        返回 InitRestoreResult(trace, session_id, allowed, reason)。
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

        # 按当前用户恢复历史摘要（群聊多用户上下文隔离），带超时降级
        _restore_id = user_id or user_openid
        if _restore_id:
            await MessageProcessorMixin._restore_user_context(self, _restore_id)

        logger.info("pipeline.restore.done proc_id={} elapsed_ms={}",
                    _proc_id, int((time.time() - _restore_t0) * 1000))
        return InitRestoreResult(trace, session_id, allowed, reason)

    async def _restore_user_context(self, _restore_id: str) -> None:
        """按当前用户恢复历史摘要（群聊多用户上下文隔离），带超时降级。

        P0 修复（用户反馈"对话链路阻塞"根因）：switch_user_context 和 restore_from_db
        曾因数据库连接竞争/锁等待阻塞 38 秒，故给两步分别加超时，超时降级跳过
        （宁可上下文不完整也不阻塞主流程）。
        P0-1 修复（QQ 会话恢复键与写库键不一致 → 突然失忆）：写库键为 qq_{openid}，
        恢复必须用同一 user_id，否则 restore_from_db 用裸 openid 查询 → DB 0 行 → 失忆。
        """
        await MessageProcessorMixin._call_with_timeout(
            self,
            self.context.switch_user_context(_restore_id),
            timeout=5.0,
            timeout_log="agent.switch_user_context_timeout",
            error_log="agent.switch_user_context_failed",
            timeout_kwargs={"user_id": _restore_id, "hint": "锁竞争或事件循环阻塞，跳过用户切换"},
        )
        if self.db:
            await MessageProcessorMixin._call_with_timeout(
                self,
                self.context.restore_from_db(
                    self.db, user_id=_restore_id,
                    address_term=self.context.current_address_term),
                timeout=10.0,
                timeout_log="agent.restore_from_db_timeout",
                error_log="agent.restore_failed",
                timeout_kwargs={"user_id": _restore_id, "hint": "数据库查询阻塞，跳过历史摘要恢复"},
            )


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
            fb_result = await self.router.fallback_chat(
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
