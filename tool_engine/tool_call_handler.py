from typing import Any
import asyncio
import fnmatch
import json
from pathlib import Path

from loguru import logger
from .tool_executor import ToolExecutor, ToolResult
from .tool_repair import ToolCallRepair
from utils.text_utils import smart_truncate
from emotion.emoji_config import get_status_msg
from config import ERROR_RULE_STRICT_MODE
from core.background_tasks import _spawn
from core.error_codes import ErrorCodeEnum
from core.event_bus import event_bus, AgentEvent, AgentEventType
from security.instruction_hierarchy import (
    InstructionLevel,
    format_instruction,
    sanitize_external_content,
)


# 写操作工具集合：这些工具会修改文件系统/配置，需进行路径白名单校验
_WRITE_TOOLS: set[str] = {
    "write_file", "delete_file", "modify_config", "install_package",
}


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


def _sanitize_tool_result(text: str) -> str:
    """清理工具结果并标记为 EXTERNAL 级别 (防 prompt injection)。

    工具返回的内容 (特别是 web_browse/file_read 等外部数据) 经过
    sanitize_external_content 清理注入模式后, 用 format_instruction
    标记为 EXTERNAL 级别 (最低优先级), 防止外部内容覆盖系统指令。
    """
    if not text:
        return text or ""
    sanitized = sanitize_external_content(text)
    return format_instruction(sanitized, InstructionLevel.EXTERNAL)


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
            except Exception as e:
                logger.warning(f"工具调用状态回调通知失败: {e}")

    async def _notify_tool_status(self, tool_name: str, stage: str, detail: str = "") -> None:
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
                data={"tool_name": tool_name, "detail": detail[:100] if detail else ""},
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
            })
        except Exception as e:
            logger.debug(f"tool_status_push_failed: {e}")

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
                     user_id: str = "",
                     skip_summarize: bool = False) -> tuple[str, list]:
        if not tool_calls:
            return self._clean_reply(assistant_content), []

        if reasoning_content:
            pass
            # scavenge 已移除：DSML 从 content 中已完整解析出工具调用列表，
            # reasoning_content 中的思考过程文本不应再提取为额外工具调用，否则会导致重复执行

        tool_results = []
        tool_messages = []
        assistant_msg = {"role": "assistant", "content": assistant_content, "tool_calls": tool_calls}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content

        messages.append(assistant_msg)

        if tool_calls:
            display_names = [TOOL_DISPLAY_NAMES.get(tc["function"]["name"], tc["function"]["name"]) for tc in tool_calls]
            logger.info(f"tool.calls_selected tools={[tc['function']['name'] for tc in tool_calls]} user_input={current_user_input[:80]}")
            # 只对耗时/重要工具显示进度，简单查询跳过
            important_tools = {"shell_command", "python_executor", "web_search", "multi_search", "web_browse", "document_reader"}
            has_important = any(tc["function"]["name"] in important_tools for tc in tool_calls)
            if has_important:
                await self._notify_status(f"{get_status_msg(self._agent_name, 'using', '、'.join(display_names[:3]), self._personality_file)}{'等' if len(display_names) > 3 else ''}")

        exec_tasks = [self._execute_single_tool(tc, trace, user_id=user_id, safe_mode=safe_mode)
                      for tc in tool_calls]
        exec_results = await asyncio.gather(*exec_tasks, return_exceptions=True)
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
            final_reply = await self._summarize_results(current_user_input, tool_results, tool_calls, trace, user_openid=user_openid, session_id=session_id)
        rc = assistant_msg.get("reasoning_content", "")
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
            await self._notify_tool_status(t_name, "started")
            try:
                if self._tool_execute_callback:
                    result = await self._tool_execute_callback(t_name, t_args, user_id=user_id, safe_mode=safe_mode)
                else:
                    result = await self._tool_executor.execute(t_name, t_args, safe_mode=safe_mode)
            except Exception as e:
                await self._notify_tool_status(t_name, "failed", detail=str(e)[:100])
                raise

            # 处理委托请求（DelegationRequest dataclass 或旧格式字符串前缀）
            result = await self._handle_delegation(result)

            result_text = ""
            if result.success:
                result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
                await self._notify_tool_status(t_name, "completed")
            else:
                result_text = f"错误: {result.error}"
                await self._notify_tool_status(t_name, "failed", detail=str(result.error)[:100])
                # P5: 工具失败后异步触发规则提取（不阻塞主流程）
                if self._error_pipeline is not None and result.error:
                    try:
                        _spawn(self._error_pipeline.extract_rule(t_name, t_args, result.error))
                    except Exception as e:
                        trace.warning("error_rule.spawn_failed", error=str(e))

            # S7: 工具结果标记为 EXTERNAL 级别并清理注入内容 (防 prompt injection)
            result_text = _sanitize_tool_result(result_text)
            return tc["id"], result, result_text, display_name

    async def _check_error_rules(self, t_name: Any, t_args: Any, tc: Any, trace: Any, display_name: Any) -> Any:
        """检查历史失败规则，返回阻塞元组或 None。"""
        try:
            matched_rules = await self._error_pipeline.check_rules(t_name, t_args)
        except Exception as e:
            trace.warning("error_rule.check_failed", error=str(e))
            matched_rules = []
        if not matched_rules:
            return None

        rule = matched_rules[0]
        rule_text = rule.get("rule_text", "") or ""
        rule_id = rule.get("id")
        try:
            await self._error_pipeline.increment_hit_count(rule_id)
        except Exception:
            logger.debug("tool_call_handler.increment_hit_count_failed", exc_info=True)
        logger.warning("error_rule.hit", tool_name=t_name, rule_id=rule_id, rule_text=rule_text)
        if ERROR_RULE_STRICT_MODE:
            trace.warning("error_rule.blocked", tool=t_name, rule_id=rule_id)
            blocked_msg = f"根据历史失败经验已拦截：{rule_text}"
            return (tc["id"], ToolResult.fail(blocked_msg), f"错误: {blocked_msg}", display_name)
        return None

    async def _handle_delegation(self, result: Any) -> Any:
        """处理工具结果中的委托请求（Klee 委托等）。"""
        from core.delegation import DelegationRequest
        if not (result.success and result.data):
            return result

        delegation_req = None
        if isinstance(result.data, DelegationRequest):
            delegation_req = result.data
        elif isinstance(result.data, str) and result.data.startswith("[KLEE_PENDING]"):
            delegation_req = DelegationRequest(
                type="xiaoli", question=result.data[len("[KLEE_PENDING]"):], delegator="xiaoda"
            )

        if delegation_req and delegation_req.type == "xiaoli" and self._xiaoli_delegate:
            xiaoli_reply = await self._xiaoli_delegate(delegation_req.question)
            result = ToolResult.ok(xiaoli_reply)
        return result

    async def _summarize_results(self, user_input: str, tool_results: list,
                                  tool_calls: list, trace: Any,
                                  user_openid: str = "", session_id: str = "") -> str:
        parts = []
        for tc, result in zip(tool_calls, tool_results, strict=False):
            if result.success and result.data:
                data_str = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
                # S7: 工具结果标记为 EXTERNAL 级别并清理注入内容 (防 prompt injection)
                parts.append(_sanitize_tool_result(data_str))
            elif not result.success:
                name = TOOL_DISPLAY_NAMES.get(tc["function"]["name"], tc["function"]["name"])
                parts.append(f"⚠️ {name}执行失败: {result.error}")
        if not parts:
            return DEGRADED_REPLY
        combined = "\n\n".join(parts)

        if not self._router:
            return combined

        try:
            xiaoda_prompt = ""
            if self._context:
                xiaoda_prompt = self._context.get_xiaoda_prompt()

            address_term = (self._context.current_address_term
                            if self._context else "爸爸") or "爸爸"
            summary_prompt = (
                f"{address_term}刚才问的是：{user_input}\n\n"
                f"工具查到结果了！你看看这些信息，帮人家整理一下回复给{address_term}吧～\n\n"
                f"记住：人家是在跟{address_term}聊天，不是在写报告！\n"
                f"- 用平时聊天的语气说话，就像面对面跟{address_term}说一样\n"
                "- 绝对不要用 **加粗**、*列表符号*、##标题 这种格式，就纯文字聊天\n"
                "- 不要用「总体概述」「团队成员汇报」这种官方词儿\n"
                "- 数据自然地嵌在句子里就行，比如「CPU温度45度呢」「内存用了60%」\n"
                "- 如果信息比较多，分几段说，每段一个话题\n"
                "- 结尾可以加一句关心的话\n\n"
                f"工具返回的结果：\n{combined}"
            )

            # 独立超时保护：summarize 最多 10s，不占用核心预算
            summary = await asyncio.wait_for(
                self._router.route(
                    "chat",
                    [
                        {"role": "system", "content": xiaoda_prompt},
                        {"role": "user", "content": user_input},
                        {"role": "user", "content": summary_prompt},
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
        except Exception as e:
            trace.error("tool.summarize_failed", error=str(e))

        return smart_truncate(combined, 2000)
