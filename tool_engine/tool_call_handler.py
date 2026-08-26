import asyncio
import fnmatch
import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from agent_core._shared import (
    ALLOWED_NON_MASTER_TOOLS as _ALLOWED_NON_MASTER_TOOLS,
)
from agent_core._shared import (
    _current_request_ctx,
    is_degraded_reply,
)
from config import ERROR_RULE_STRICT_MODE
from core.background_tasks import _spawn
from core.error_codes import ErrorCodeEnum
from core.event_bus import AgentEvent, AgentEventType, event_bus
from emotion.emoji_config import get_status_msg
from security.instruction_hierarchy import (
    InstructionLevel,
    format_instruction,
    sanitize_external_content,
)

from .tool_executor import ToolExecutor, ToolResult
from .tool_repair import ToolCallRepair

# 写操作工具集合：这些工具会修改文件系统/配置，需进行路径白名单校验
_WRITE_TOOLS: set[str] = {
    "write_file", "delete_file", "modify_config", "install_package",
}


def _strip_tool_metadata(text: str) -> str:
    """移除 raw tool output 中的内部元数据，避免直接展示给用户。

    处理 recall/memory 等工具输出中的:
    - (ID:123 重要度:0.3 相关度:2.27)
    - 摘要（297字）：
    """
    # (ID:xxx 重要度:x.xx 相关度:x.xx)
    text = re.sub(r'\(ID:\d+\s+重要度:[\d.]+\s+相关度:[\d.]+\)', '', text)
    # 摘要（xxx字）：
    text = re.sub(r'摘要（\d+字）[：:]?\s*', '', text)
    # 清理多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_path_from_args(tool_name: str, args: dict) -> str:
    """从工具参数中提取目标路径。

    write_file 使用 input_str="path|||content" 格式；
    其他工具按常见参数名（path / file_path / target_path）查找。
    """
    if tool_name == "write_file":
        input_str = args.get("input_str", "") or ""
        if "|||" in input_str:
            return input_str.split("|||", 1)[0]
        return input_str
    for key in ("path", "file_path", "target_path"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _sanitize_tool_result(text: str, tool_name: str = "") -> str:
    """清理工具结果并标记为 EXTERNAL 级别 (防 prompt injection)。

    工具返回的内容 (特别是 web_browse/file_read 等外部数据) 经过
    sanitize_external_content 清理注入模式后, 用 format_instruction
    标记为 EXTERNAL 级别 (最低优先级), 防止外部内容覆盖系统指令。

    记忆工具 (recall/remember/forget/confirm_memory/correct_memory) 返回的是
    用户自己的记忆数据，属于可信内容，不标记为"不可信外部数据"。
    """
    if not text:
        return text or ""
    # 记忆工具返回的是用户自己的记忆，属于可信内容
    if tool_name in _TRUSTED_MEMORY_TOOLS:
        return text
    sanitized = sanitize_external_content(text)
    return format_instruction(sanitized, InstructionLevel.EXTERNAL)


# 记忆工具白名单：返回的是用户自己的记忆数据，属于可信内容
_TRUSTED_MEMORY_TOOLS: frozenset[str] = frozenset({
    "recall", "remember", "forget", "confirm_memory", "correct_memory",
})


DEGRADED_REPLY = "嗯……人家现在有点不太舒服，等会儿再聊好不好？"

TOOL_DISPLAY_NAMES = {
    "shell_command": "Shell命令",
    "list_files": "文件列表",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "search_files": "搜索文件",
    "get_current_time": "查询时间",
    "python_executor": "Python代码",
    "calculator": "计算器",
    "delegate_task": "委托子代理",
    "web_search": "网络搜索",
    "get_weather": "查询天气",
    "document_reader": "读取文档",
    "web_browse": "浏览网页",
    "multi_search": "多引擎搜索",
    "wolfram_query": "知识计算",
}


class ToolCallHandler:
    """工具调用处理器，协调执行、修复与回调钩子。"""

    def __init__(self, tool_executor: ToolExecutor, tool_repair: ToolCallRepair,
                 clean_reply_callback: Any, context: Any=None, router: Any=None, xiaoli_delegate: Any=None,
                 status_callback: Any=None, agent_name: str = "xiaoda", personality_file: str | None = None,
                 tool_execute_callback: Any=None, error_pipeline: Any=None,
                 agent_config: Any=None) -> None:
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._clean_reply = clean_reply_callback
        self._context = context
        self._router = router
        self._xiaoli_delegate = xiaoli_delegate
        self._status_callback = status_callback
        self._agent_name = agent_name
        self._personality_file = personality_file
        self._tool_execute_callback = tool_execute_callback  # 带钩子的工具执行回调
        self._error_pipeline = error_pipeline  # P5: 失败经验→规则闭环（可选）
        self._exec_semaphore = asyncio.Semaphore(5)  # 限制并发工具执行数
        self._agent_config = agent_config  # SubAgentConfig（子代理路径白名单校验用）

    def set_agent_config(self, agent_config: Any) -> None:
        """注入 SubAgentConfig（子代理路径白名单校验用）。"""
        self._agent_config = agent_config

    def set_status_callback(self, callback: Any) -> None:
        self._status_callback = callback

    def set_error_pipeline(self, pipeline: Any) -> None:
        """注入 ErrorRulePipeline（P5）。允许 bootstrap 阶段延后注入。"""
        self._error_pipeline = pipeline

    async def _notify_status(self, message: str) -> None:
        if self._status_callback:
            try:
                await self._status_callback(message)
            except (RuntimeError, OSError, ConnectionError) as e:
                logger.warning("工具调用状态回调通知失败: {}", e)

    async def _notify_tool_status(
        self,
        tool_name: str,
        stage: str,
        detail: str = "",
        *,
        tool_call_id: str = "",
        turn: int = 0,
        index: int = 0,
    ) -> None:
        """推送工具调用的中间状态 — 通过 EventBus 发射 TOOL_* 事件。

        Args:
            tool_name: 工具名称，如 "web_search"、"memory_search"
            stage: "started" / "completed" / "failed"
            detail: 详细信息
        """
        from config import STREAM_TOOL_STATUS
        if not STREAM_TOOL_STATUS:
            return

        # EventBus 事件发射（统一事件通道）
        stage_to_type = {
            "started": AgentEventType.TOOL_STARTED,
            "completed": AgentEventType.TOOL_COMPLETED,
            "failed": AgentEventType.TOOL_FAILED,
        }
        event_type = stage_to_type.get(stage)
        if event_type:
            await event_bus.emit(AgentEvent(
                type=event_type,
                agent=getattr(self, "_agent_name", ""),
                task_id=getattr(self, "_task_id", ""),
                data={
                    "tool_name": tool_name,
                    "detail": detail[:100] if detail else "",
                    "tool_call_id": tool_call_id,
                    "turn": turn,
                    "index": index,
                },
            ))

        # 保留 status_callback 兜底（向后兼容）
        if not self._status_callback:
            return
        stage_labels = {"started": "正在调用", "completed": "完成", "failed": "失败"}
        label = stage_labels.get(stage, stage)
        display = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
        try:
            await self._status_callback({
                "type": "tool_status",
                "tool": tool_name,
                "stage": stage,
                "label": f"{label} {display}...",
                "detail": detail[:100] if detail else "",
                "tool_call_id": tool_call_id,
                "turn": turn,
                "index": index,
            })
        except (RuntimeError, OSError, ConnectionError) as e:
            logger.debug("tool_status_push_failed: {}", e)

    def _check_path_whitelist(self, path: str, agent_config: Any = None) -> tuple[bool, str]:
        """检查路径是否在子代理白名单内。

        校验顺序：黑名单优先 → 白名单为空表示允许所有 → 白名单匹配。
        路径使用 POSIX 风格分隔符进行 glob 匹配，兼容 Windows。

        :param path: 待校验的路径
        :param agent_config: SubAgentConfig（为 None 表示主体 Agent，允许所有）
        :returns: (allowed, reason)
        """
        if not agent_config:
            return True, "no agent config (main agent)"

        # 规范化为 POSIX 风格路径，跨平台一致匹配
        norm_path = str(Path(path)).replace("\\", "/")

        # 1. 黑名单优先
        for forbidden in agent_config.forbidden_paths or []:
            if fnmatch.fnmatch(norm_path, forbidden):
                return False, f"path matches forbidden pattern: {forbidden}"

        # 2. 白名单为空表示允许所有
        if not agent_config.allowed_paths:
            return True, "no whitelist restriction"

        # 3. 白名单匹配
        for allowed in agent_config.allowed_paths:
            if fnmatch.fnmatch(norm_path, allowed):
                return True, f"path matches allowed pattern: {allowed}"

        return False, "path not in whitelist"


    async def handle(self, tool_calls: list[dict], messages: list[dict],
                     trace: Any, *, assistant_content: str = "",
                     reasoning_content: str | None = None,
                     user_openid: str = "", session_id: str = "",
                     safe_mode: bool = False,
                     current_user_input: str = "",
                     user_id: str = "", skip_summarize: bool = False) -> tuple[str, list]:
        from core.event_bus import gen_task_id
        self._task_id = gen_task_id(
            agent=self._agent_name,
            input_hint=current_user_input[:50] if current_user_input else "",
        )
        if not tool_calls:
            return self._clean_reply(assistant_content), []

        tool_results = []
        tool_messages = []
        assistant_calls = [
            {key: value for key, value in call.items() if not key.startswith("_stream_")}
            for call in tool_calls
        ]
        assistant_msg = {
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": assistant_calls,
        }
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content

        messages.append(assistant_msg)

        display_names = [TOOL_DISPLAY_NAMES.get(tc["function"]["name"], tc["function"]["name"]) for tc in tool_calls]
        logger.info("tool.calls_selected tools={} user_input={}", [tc['function']['name'] for tc in tool_calls], current_user_input[:80])
        # 只对耗时/重要工具显示进度，简单查询跳过
        important_tools = {"shell_command", "python_executor", "web_search", "multi_search", "web_browse", "document_reader"}
        has_important = any(tc["function"]["name"] in important_tools for tc in tool_calls)
        if has_important:
            await self._notify_status(f"{get_status_msg(self._agent_name, 'using', '、'.join(display_names[:3]), self._personality_file)}{'等' if len(display_names) > 3 else ''}")

        _concurrent_count = len(tool_calls)
        _exec_start = time.time()
        logger.info("tool.concurrent_exec_start",
                    count=_concurrent_count,
                    tools=[tc["function"]["name"] for tc in tool_calls])
        exec_tasks = [self._execute_single_tool(tc, trace, user_id=user_id, safe_mode=safe_mode)
                      for tc in tool_calls]
        exec_results = await asyncio.gather(*exec_tasks, return_exceptions=True)
        _exec_elapsed = round(time.time() - _exec_start, 2)
        logger.info("tool.concurrent_exec_done",
                    count=_concurrent_count, elapsed=_exec_elapsed,
                    tools=[tc["function"]["name"] for tc in tool_calls])
        for idx, er in enumerate(exec_results):
            if isinstance(er, Exception):
                trace.warning("tool.exec_exception", error=str(er)[:200])
                # 添加失败结果，确保 tool_results 与 tool_calls 一一对应
                tcid = tool_calls[idx]["id"] if idx < len(tool_calls) else f"err_{idx}"
                tool_results.append(ToolResult.fail(f"执行异常: {er}"))
                tool_messages.append({"role": "tool", "tool_call_id": tcid, "content": "错误: 执行异常"})
                continue
            if not isinstance(er, (tuple, list)) or len(er) != 4:
                trace.warning("tool.unexpected_result", type=type(er).__name__)
                tcid = tool_calls[idx]["id"] if idx < len(tool_calls) else f"err_{idx}"
                tool_results.append(ToolResult.fail("工具返回格式异常"))
                tool_messages.append({"role": "tool", "tool_call_id": tcid, "content": "错误: 工具返回格式异常"})
                continue
            tcid, res, rtext, _dname = er
            tool_results.append(res)
            tool_messages.append({"role": "tool", "tool_call_id": tcid, "content": rtext})

        messages.extend(tool_messages)

        # skip_summarize=True：验收循环模式，工具结果已追加到 messages，
        # 跳过 summarize 和上下文记录，由调用方决定下一步
        if skip_summarize:
            return "", tool_results

        # 工具全部失败（被 hooks 拦截）时跳过 _summarize_results，避免第二次 LLM 调用导致超时
        # 直接用 LLM 第一次的文本回复（assistant_content）
        _all_failed = all(not r.success for r in tool_results) if tool_results else True
        if _all_failed and assistant_content.strip():
            final_reply = self._clean_reply(assistant_content)
            trace.info("tool.all_failed_skip_summarize", tool_count=len(tool_results))
        else:
            # P0 修复：传入 messages（已含 assistant(tool_calls) + tool(result) 消息），
            # 让 _summarize_results 复用完整上下文，不再凭空 summarize 导致瞎扯
            final_reply = await self._summarize_results(
                current_user_input, tool_results, tool_calls, trace,
                user_openid=user_openid, session_id=session_id,
                messages=messages,
            )
        rc = assistant_msg.get("reasoning_content", "")
        # 降级/错误回复不入 history 也不入记忆库（与主对话、子代理路径一致），
        # 同时跳过 user 消息避免未配对断档
        if is_degraded_reply(final_reply):
            logger.info("tool_handler.skip_degraded_reply_not_in_history", reply_preview=final_reply[:60])
        else:
            await self._context.add_message("user", current_user_input)
            await self._context.add_message("assistant", final_reply,
                                     reasoning_content=rc if rc else None)
        return final_reply, tool_results

    async def _execute_single_tool(self, tc: Any, trace: Any, *, user_id: str = "",
                                    safe_mode: bool = False) -> Any:
        """执行单个工具调用，返回 (tc_id, result, result_text, display_name)。"""
        async with self._exec_semaphore:
            t_name = tc["function"]["name"]
            t_args_str = tc["function"]["arguments"]
            display_name = TOOL_DISPLAY_NAMES.get(t_name, t_name)

            if self._tool_repair.detect_storm(t_name, t_args_str):
                trace.warning("tool.storm_detected", tool=t_name)
                return tc["id"], ToolResult.fail("该工具调用已被风暴检测拦截"), "", display_name

            repaired = self._tool_repair.repair_truncation(t_args_str)
            if repaired:
                t_args_str = repaired

            try:
                t_args = json.loads(t_args_str)
            except json.JSONDecodeError:
                t_args = {}

            # P5: 调用前检查历史失败规则
            if self._error_pipeline is not None:
                blocked = await self._check_error_rules(t_name, t_args, tc, trace, display_name)
                if blocked is not None:
                    return blocked

            # VULN-27：非主人执行层门禁。主路径的工具列表过滤
            # （message_processor._prepare_sticker_and_tools）不覆盖子代理/委托
            # 路径，非主人 @子代理 可拿到全量工具列表。这里在执行前按
            # _current_request_ctx.is_master 强制校验：非主人仅允许白名单工具。
            # 无请求上下文（内部调度/主动任务）不拦截，fail-open 仅限系统自身发起的调用。
            _ctx = _current_request_ctx.get()
            if (_ctx is not None and getattr(_ctx, "is_master", True) is False
                    and t_name not in _ALLOWED_NON_MASTER_TOOLS):
                err_code = ErrorCodeEnum.E_TOOL007
                logger.warning(
                    "tool.non_master_forbidden",
                    tool=t_name, user_id=getattr(_ctx, "user_id", ""),
                    session_id=getattr(_ctx, "session_id", ""),
                    error_code=err_code.code,
                )
                if trace is not None:
                    trace.warning("tool.non_master_forbidden", tool=t_name)
                err_msg = f"[{err_code.code}] {err_code.message}: {t_name}"
                return (tc["id"], ToolResult.fail(err_msg), f"错误: {err_msg}", display_name)

            # 子代理路径白名单校验：写操作工具执行前检查目标路径
            if t_name in _WRITE_TOOLS and self._agent_config is not None:
                target_path = _extract_path_from_args(t_name, t_args)
                if target_path:
                    allowed, reason = self._check_path_whitelist(target_path, self._agent_config)
                    if not allowed:
                        err_code = ErrorCodeEnum.E_TOOL006
                        logger.warning(
                            "tool.path_forbidden",
                            agent=getattr(self._agent_config, "name", "unknown"),
                            path=target_path,
                            reason=reason,
                            error_code=err_code.code,
                        )
                        trace.warning("tool.path_forbidden", tool=t_name,
                                      path=target_path, reason=reason)
                        err_msg = f"[{err_code.code}] {err_code.message}: {reason}"
                        return (tc["id"], ToolResult.fail(err_msg), f"错误: {err_msg}", display_name)

            # 优先使用带钩子的工具执行回调，否则直接执行
            status_meta = {
                "tool_call_id": str(tc.get("id", "")),
                "turn": int(tc.get("_stream_turn", 0) or 0),
                "index": int(tc.get("_stream_index", 0) or 0),
            }
            await self._notify_tool_status(t_name, "started", **status_meta)
            _tool_start = time.time()
            try:
                if self._tool_execute_callback:
                    result = await self._tool_execute_callback(t_name, t_args, user_id=user_id, safe_mode=safe_mode)
                else:
                    result = await self._tool_executor.execute(t_name, t_args, safe_mode=safe_mode)
            except (RuntimeError, OSError, ValueError, TimeoutError, KeyError) as e:
                _tool_elapsed = round(time.time() - _tool_start, 2)
                logger.warning("tool.exec_failed", tool=t_name, elapsed=_tool_elapsed, error=str(e)[:100])
                await self._notify_tool_status(
                    t_name, "failed", detail=str(e)[:100], **status_meta,
                )
                raise

            _tool_elapsed = round(time.time() - _tool_start, 2)
            logger.info("tool.exec_done", tool=t_name, elapsed=_tool_elapsed, success=result.success)

            # 处理委托请求（DelegationRequest dataclass 或旧格式字符串前缀）
            result = await self._handle_delegation(result)

            result_text = ""
            if result.success:
                result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
                await self._notify_tool_status(t_name, "completed", **status_meta)
            else:
                result_text = f"错误: {result.error}"
                await self._notify_tool_status(
                    t_name, "failed", detail=str(result.error)[:100], **status_meta,
                )
                # P5: 工具失败后异步触发规则提取（不阻塞主流程）
                if self._error_pipeline is not None and result.error:
                    try:
                        _spawn(self._error_pipeline.extract_rule(t_name, t_args, result.error))
                    except (RuntimeError, ValueError) as e:
                        trace.warning("error_rule.spawn_failed", error=str(e))

            # S7: 工具结果标记为 EXTERNAL 级别并清理注入内容 (防 prompt injection)
            result_text = _sanitize_tool_result(result_text, t_name)
            return tc["id"], result, result_text, display_name

    async def _check_error_rules(self, t_name: Any, t_args: Any, tc: Any, trace: Any, display_name: Any) -> Any:
        """检查历史失败规则，返回阻塞元组或 None。"""
        try:
            matched_rules = await self._error_pipeline.check_rules(t_name, t_args)
        except (RuntimeError, OSError, ValueError) as e:
            trace.warning("error_rule.check_failed", error=str(e))
            matched_rules = []
        if not matched_rules:
            return None

        rule = matched_rules[0]
        rule_text = rule.get("rule_text", "") or ""
        rule_id = rule.get("id")
        try:
            await self._error_pipeline.increment_hit_count(rule_id)
        except (OSError, RuntimeError):
            logger.debug("tool_call_handler.increment_hit_count_failed", exc_info=True)
        logger.warning("error_rule.hit", tool_name=t_name, rule_id=rule_id, rule_text=rule_text)
        if ERROR_RULE_STRICT_MODE:
            trace.warning("error_rule.blocked", tool=t_name, rule_id=rule_id)
            blocked_msg = f"根据历史失败经验已拦截：{rule_text}"
            return (tc["id"], ToolResult.fail(blocked_msg), f"错误: {blocked_msg}", display_name)
        return None

    async def _handle_delegation(self, result: Any) -> Any:
        """处理工具结果中的委托请求（delegate_task → xiaoli）。"""
        from core.delegation import DelegationRequest
        if not (result.success and result.data):
            return result

        delegation_req = None
        if isinstance(result.data, DelegationRequest):
            delegation_req = result.data

        if delegation_req and delegation_req.type == "xiaoli" and self._xiaoli_delegate:
            xiaoli_reply = await self._xiaoli_delegate(delegation_req.question)
            result = ToolResult.ok(xiaoli_reply)
        return result

    async def _summarize_results(self, user_input: str, tool_results: list,
                                  tool_calls: list, trace: Any,
                                  user_openid: str = "", session_id: str = "",
                                  messages: list[dict] | None = None,
                                  assistant_content: str = "") -> str:
        """基于工具结果生成最终回复。

        P0 修复（工具调用后 LLM 瞎扯/出戏 根因）：
        原实现把 `summary_prompt`（含"用平时聊天的语气""不要用加粗"等元指令）
        作为 **user message** 注入全新上下文 `[system, user:user_input, user:summary_prompt]`，
        导致两个严重问题：
          1. **上下文污染**：元指令作为 user 消息进入 LLM 可见上下文，后续轮次 LLM 会
             回应"用平时聊天的语气"等元词汇，造成角色出戏（详见 conversation_logs 案例）。
          2. **上下文割裂**：丢弃了 verification loop 已有的对话历史 + 工具结果
             （`role: tool` 消息），LLM 失去对话连贯性，凭空 summarize → 瞎扯。

        修复策略（双层）：
          - 主路径（messages 非空）：复用 verification loop 的 `messages`（已含
            system+history+assistant(tool_calls)+tool(result)），仅追加一条 **system**
            角色的简短提示，让 LLM 基于完整上下文 + 工具结果自然续写。
          - 兜底路径（messages 为空，向后兼容）：保留独立调用，但 `summary_prompt`
             改为 **system** 角色（不再作为 user 消息污染上下文）。

        硬约束（沿用）：只能基于工具结果回答，不编造。
        """
        parts = []
        for tc, result in zip(tool_calls, tool_results, strict=False):
            if result.success and result.data:
                data_str = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
                # S7: 工具结果标记为 EXTERNAL 级别并清理注入内容 (防 prompt injection)
                t_name = tc["function"]["name"]
                parts.append(_sanitize_tool_result(data_str, t_name))
            elif not result.success:
                name = TOOL_DISPLAY_NAMES.get(tc["function"]["name"], tc["function"]["name"])
                parts.append(f"⚠️ {name}执行失败: {result.error}")
        if not parts:
            return DEGRADED_REPLY
        combined = "\n\n".join(parts)

        if not self._router:
            return combined

        address_term = (self._context.current_address_term
                        if self._context else "爸爸") or "爸爸"

        # 简短 system 提示（不再作为 user 消息污染上下文）
        # 设计原则：
        #   - 只给"基于工具结果回复"的硬约束，不给"用什么语气"等元指令（避免出戏）
        #   - 语气/格式由 system prompt（SOUL.md/IDENTITY.md）统一管理，不在此重复
        #   - 要求明确表达，避免模糊表述（如"数据加载完毕"应说明加载了什么）
        _grounding_hint = (
            f"[系统提示] 上方工具已返回结果（role=tool 消息）。请基于这些工具结果，"
            f"用你本来的语气自然回复{address_term}。"
            f"硬约束：只能基于工具返回的信息回答，不要添加、推测或编造结果中没有的信息；"
            f"如果工具结果不足以回答，请如实说明。"
            f"表达要求：先明确说明执行了什么操作，再描述具体结果，避免模糊表述。"
        )

        try:
            if messages is not None and len(messages) > 0:
                # 主路径：复用 verification loop 上下文（已含工具结果 role=tool 消息）
                # 仅追加 system 提示，不追加任何 user 消息 → 不污染上下文
                # 注意：messages 是调用方传入的引用，这里 copy 一份避免修改原列表
                summarize_messages = list(messages)
                summarize_messages.append({"role": "system", "content": _grounding_hint})
                summary = await asyncio.wait_for(
                    self._router.route(
                        "chat",
                        summarize_messages,
                        temperature=0.6,
                        user_openid=user_openid,
                        session_id=session_id,
                    ),
                    timeout=10,
                )
            else:
                # 兜底路径（向后兼容，无 messages 时使用）
                # 修复：summary_prompt 改为 system 角色（原为 user 角色导致污染）
                xiaoda_prompt = ""
                if self._context:
                    xiaoda_prompt = await asyncio.to_thread(self._context.get_xiaoda_prompt)
                summary_prompt = (
                    f"{address_term}刚才问的是：{user_input}\n\n"
                    f"工具查到结果了！请基于以下结果用你本来的语气回复{address_term}。\n\n"
                    "⚠️ 硬约束：只能基于以下工具返回的结果回答，绝对不要添加、推测或编造结果中没有的信息！\n\n"
                    f"工具返回的结果：\n{combined}"
                )
                summary = await asyncio.wait_for(
                    self._router.route(
                        "chat",
                        [
                            {"role": "system", "content": xiaoda_prompt},
                            {"role": "system", "content": summary_prompt},
                            {"role": "user", "content": user_input},
                        ],
                        temperature=0.6,
                        user_openid=user_openid,
                        session_id=session_id,
                    ),
                    timeout=10,
                )
            if isinstance(summary, str) and summary.strip():
                return self._clean_reply(summary)
        except TimeoutError:
            logger.warning("tool.summarize_timeout", tool_count=len(tool_results))
        except (RuntimeError, ValueError, OSError) as e:
            trace.error("tool.summarize_failed", error=str(e))

        # P0 修复：summarize 超时/失败时绝不返回原始工具结果当回复。
        # 原实现 return smart_truncate(combined) → 把 B站搜索结果原文（标题/链接/摘要）
        # 直接倒给用户，还追加"——人家说到一半啦"标记，体验极差。
        # 修复优先级：LLM 已生成文本 > 友好降级提示，绝不返回原始工具数据。
        if assistant_content and assistant_content.strip():
            _cleaned = self._clean_reply(assistant_content)
            if _cleaned.strip():
                trace.info("tool.summarize_fallback_assistant_content")
                return _cleaned
        trace.warning("tool.summarize_fallback_degraded")
        return DEGRADED_REPLY
