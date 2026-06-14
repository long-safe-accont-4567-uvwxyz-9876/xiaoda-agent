import asyncio
import json

from loguru import logger
from .tool_executor import ToolExecutor, ToolResult
from .tool_repair import ToolCallRepair
from .tool_registry import get_tool, to_openai_tools
from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls, smart_truncate
from emotion.emoji_config import get_status_msg


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
                 tool_execute_callback=None):
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
        self._exec_semaphore = asyncio.Semaphore(5)  # 限制并发工具执行数

    def set_status_callback(self, callback):
        self._status_callback = callback

    async def _notify_status(self, message: str):
        if self._status_callback:
            try:
                await self._status_callback(message)
            except Exception as e:
                logger.warning(f"工具调用状态回调通知失败: {e}")

    async def handle(self, tool_calls: list[dict], messages: list[dict],
                     trace, *, assistant_content: str = "",
                     reasoning_content: str | None = None,
                     user_openid: str = "", session_id: str = "",
                     safe_mode: bool = False,
                     current_user_input: str = "",
                     user_id: str = "") -> tuple[str, list]:
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

                # 优先使用带钩子的工具执行回调，否则直接执行
                if self._tool_execute_callback:
                    result = await self._tool_execute_callback(t_name, t_args, user_id=user_id, safe_mode=safe_mode)
                else:
                    result = await self._tool_executor.execute(t_name, t_args, safe_mode=safe_mode)

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
                else:
                    result_text = f"错误: {result.error}"

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

            summary_prompt = (
                f"爸爸刚才问的是：{user_input}\n\n"
                "工具查到结果了！你看看这些信息，帮人家整理一下回复给爸爸吧～\n\n"
                "记住：人家是在跟爸爸聊天，不是在写报告！\n"
                "- 用平时聊天的语气说话，就像面对面跟爸爸说一样\n"
                "- 绝对不要用 **加粗**、*列表符号*、##标题 这种格式，就纯文字聊天\n"
                "- 不要用「总体概述」「团队成员汇报」这种官方词儿\n"
                "- 数据自然地嵌在句子里就行，比如「CPU温度45度呢」「内存用了60%」\n"
                "- 如果信息比较多，分几段说，每段一个话题\n"
                "- 结尾可以加一句关心的话\n\n"
                f"工具返回的结果：\n{combined}"
            )

            summary = await self._router.route(
                "chat",
                [
                    {"role": "system", "content": nahida_prompt},
                    {"role": "user", "content": user_input},
                    {"role": "user", "content": summary_prompt},
                ],
                temperature=0.6,
                user_openid=user_openid,
                session_id=session_id,
            )
            if isinstance(summary, str) and summary.strip():
                return self._clean_reply(summary)
        except Exception as e:
            trace.error("tool.summarize_failed", error=str(e))

        return smart_truncate(combined, 2000)
