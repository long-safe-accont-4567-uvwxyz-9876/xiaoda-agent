import asyncio
import json

from loguru import logger
from .tool_executor import ToolExecutor, ToolResult
from .tool_repair import ToolCallRepair
from .tool_registry import get_tool, to_openai_tools
from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls, smart_truncate
from emotion.emoji_config import get_status_msg
from config import ERROR_RULE_STRICT_MODE
from core.background_tasks import _spawn


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
    def __init__(self, tool_executor: ToolExecutor, tool_repair: ToolCallRepair,
                 clean_reply_callback, context=None, router=None, klee_delegate=None,
                 status_callback=None, agent_name: str = "nahida", personality_file: str | None = None,
                 tool_execute_callback=None, error_pipeline=None):
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._clean_reply = clean_reply_callback
        self._context = context
        self._router = router
        self._klee_delegate = klee_delegate
        self._status_callback = status_callback
        self._agent_name = agent_name
        self._personality_file = personality_file
        self._tool_execute_callback = tool_execute_callback  # 带钩子的工具执行回调
        self._error_pipeline = error_pipeline  # P5: 失败经验→规则闭环（可选）
        self._exec_semaphore = asyncio.Semaphore(5)  # 限制并发工具执行数

    def set_status_callback(self, callback):
        self._status_callback = callback

    def set_error_pipeline(self, pipeline) -> None:
        """注入 ErrorRulePipeline（P5）。允许 bootstrap 阶段延后注入。"""
        self._error_pipeline = pipeline

    async def _notify_status(self, message: str):
        if self._status_callback:
            try:
                await self._status_callback(message)
            except Exception as e:
                logger.warning(f"工具调用状态回调通知失败: {e}")

    async def _notify_tool_status(self, tool_name: str, stage: str, detail: str = ""):
        """推送工具调用的中间状态（P0）。

        Args:
            tool_name: 工具名称，如 "web_search"、"memory_search"
            stage: "started" / "completed" / "failed"
            detail: 详细信息
        """
        from config import STREAM_TOOL_STATUS
        if not STREAM_TOOL_STATUS or not self._status_callback:
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

    async def handle(self, tool_calls: list[dict], messages: list[dict],
                     trace, *, assistant_content: str = "",
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

        async def _exec_one(tc):
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
                    try:
                        matched_rules = await self._error_pipeline.check_rules(t_name, t_args)
                    except Exception as e:
                        trace.warning("error_rule.check_failed", error=str(e))
                        matched_rules = []
                    if matched_rules:
                        rule = matched_rules[0]
                        rule_text = rule.get("rule_text", "") or ""
                        rule_id = rule.get("id")
                        # 累计 hit_count（失败安全）
                        try:
                            await self._error_pipeline.increment_hit_count(rule_id)
                        except Exception:
                            pass
                        logger.warning("error_rule.hit",
                                       tool_name=t_name, rule_id=rule_id,
                                       rule_text=rule_text)
                        if ERROR_RULE_STRICT_MODE:
                            trace.warning("error_rule.blocked",
                                          tool=t_name, rule_id=rule_id)
                            blocked_msg = f"根据历史失败经验已拦截：{rule_text}"
                            return (tc["id"],
                                    ToolResult.fail(blocked_msg),
                                    f"错误: {blocked_msg}",
                                    display_name)
                        # 非严格模式：仅记录警告，继续执行

                # 优先使用带钩子的工具执行回调，否则直接执行
                await self._notify_tool_status(t_name, "started")
                try:
                    if self._tool_execute_callback:
                        result = await self._tool_execute_callback(t_name, t_args, user_id=user_id, safe_mode=safe_mode)
                    else:
                        result = await self._tool_executor.execute(t_name, t_args, safe_mode=safe_mode)
                except Exception as e:
                    # 工具执行抛异常时推送 failed 状态，避免前端卡在"正在调用 X..."
                    await self._notify_tool_status(t_name, "failed", detail=str(e)[:100])
                    raise

                # 处理委托请求（DelegationRequest dataclass 或旧格式字符串前缀）
                from core.delegation import DelegationRequest
                if result.success and result.data:
                    delegation_req = None
                    if isinstance(result.data, DelegationRequest):
                        delegation_req = result.data
                    elif isinstance(result.data, str) and result.data.startswith("[KLEE_PENDING]"):
                        # 兼容旧格式
                        delegation_req = DelegationRequest(
                            type="klee", question=result.data[len("[KLEE_PENDING]"):], delegator="nahida"
                        )

                    if delegation_req and delegation_req.type == "klee":
                        if self._klee_delegate:
                            klee_reply = await self._klee_delegate(delegation_req.question)
                            result = ToolResult.ok(klee_reply)

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
                            _spawn(self._error_pipeline.extract_rule(
                                t_name, t_args, result.error))
                        except Exception as e:
                            trace.warning("error_rule.spawn_failed", error=str(e))

                return tc["id"], result, result_text, display_name

        exec_tasks = [_exec_one(tc) for tc in tool_calls]
        exec_results = await asyncio.gather(*exec_tasks, return_exceptions=True)
        for idx, er in enumerate(exec_results):
            if isinstance(er, Exception):
                trace.warning("tool.exec_exception", error=str(er)[:200])
                # 添加失败结果，确保 tool_results 与 tool_calls 一一对应
                tcid = tool_calls[idx]["id"] if idx < len(tool_calls) else f"err_{idx}"
                tool_results.append(ToolResult.fail(f"执行异常: {er}"))
                tool_messages.append({"role": "tool", "tool_call_id": tcid, "content": f"错误: 执行异常"})
                continue
            if not isinstance(er, (tuple, list)) or len(er) != 4:
                trace.warning("tool.unexpected_result", type=type(er).__name__)
                tcid = tool_calls[idx]["id"] if idx < len(tool_calls) else f"err_{idx}"
                tool_results.append(ToolResult.fail("工具返回格式异常"))
                tool_messages.append({"role": "tool", "tool_call_id": tcid, "content": "错误: 工具返回格式异常"})
                continue
            tcid, res, rtext, dname = er
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

    async def _summarize_results(self, user_input: str, tool_results: list,
                                  tool_calls: list, trace,
                                  user_openid: str = "", session_id: str = "") -> str:
        parts = []
        for tc, result in zip(tool_calls, tool_results):
            if result.success and result.data:
                data_str = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
                parts.append(data_str)
            elif not result.success:
                name = TOOL_DISPLAY_NAMES.get(tc["function"]["name"], tc["function"]["name"])
                parts.append(f"⚠️ {name}执行失败: {result.error}")
        if not parts:
            return DEGRADED_REPLY
        combined = "\n\n".join(parts)

        if not self._router:
            return combined

        try:
            nahida_prompt = ""
            if self._context:
                nahida_prompt = self._context.get_nahida_prompt()

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
                        {"role": "system", "content": nahida_prompt},
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
        except asyncio.TimeoutError:
            logger.warning("tool.summarize_timeout", tool_count=len(tool_results))
        except Exception as e:
            trace.error("tool.summarize_failed", error=str(e))

        return smart_truncate(combined, 2000)
