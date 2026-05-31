import asyncio
import json
from loguru import logger
from tool_executor import ToolExecutor, ToolResult
from tool_repair import ToolCallRepair
from tool_registry import get_tool, to_openai_tools
from text_utils import has_dsml_tool_calls, parse_dsml_tool_calls
from emoji_config import get_status_msg


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
    "call_klee": "召唤可莉",
    "web_search": "网络搜索",
    "get_weather": "查询天气",
    "document_reader": "读取文档",
    "web_browse": "浏览网页",
    "multi_search": "多引擎搜索",
}


class ToolCallHandler:
    def __init__(self, tool_executor: ToolExecutor, tool_repair: ToolCallRepair,
                 clean_reply_callback, context=None, router=None, klee_delegate=None,
                 status_callback=None, agent_name: str = "nahida", personality_file: str | None = None):
        self._tool_executor = tool_executor
        self._tool_repair = tool_repair
        self._clean_reply = clean_reply_callback
        self._context = context
        self._router = router
        self._klee_delegate = klee_delegate
        self._status_callback = status_callback
        self._agent_name = agent_name
        self._personality_file = personality_file

    def set_status_callback(self, callback):
        self._status_callback = callback

    async def _notify_status(self, message: str):
        if self._status_callback:
            try:
                await self._status_callback(message)
            except Exception:
                pass

    async def handle(self, tool_calls: list[dict], messages: list[dict],
                     trace, *, assistant_content: str = "",
                     reasoning_content: str | None = None,
                     user_openid: str = "", session_id: str = "",
                     safe_mode: bool = False,
                     current_user_input: str = "") -> tuple[str, list]:
        if not tool_calls:
            return self._clean_reply(assistant_content), []

        tool_results = []
        tool_messages = []
        assistant_msg = {"role": "assistant", "content": assistant_content, "tool_calls": tool_calls}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        messages.append(assistant_msg)

        if tool_calls:
            display_names = [TOOL_DISPLAY_NAMES.get(tc["function"]["name"], tc["function"]["name"]) for tc in tool_calls]
            important_tools = {"shell_command", "python_executor", "web_search", "multi_search", "web_browse", "document_reader"}
            has_important = any(tc["function"]["name"] in important_tools for tc in tool_calls)
            if has_important:
                await self._notify_status(f"{get_status_msg(self._agent_name, 'using', '、'.join(display_names[:3]), self._personality_file)}{'等' if len(display_names) > 3 else ''}")

        async def _exec_one(tc):
            t_name = tc["function"]["name"]
            t_args_str = tc["function"]["arguments"]
            display_name = TOOL_DISPLAY_NAMES.get(t_name, t_name)

            if self._tool_repair.detect_storm(t_name, t_args_str):
                return tc["id"], ToolResult.fail("该工具调用已被风暴检测拦截"), ""

            repaired = self._tool_repair.repair_truncation(t_args_str)
            if repaired:
                t_args_str = repaired

            try:
                t_args = json.loads(t_args_str)
            except json.JSONDecodeError:
                t_args = {}

            result = await self._tool_executor.execute(t_name, t_args, safe_mode=safe_mode)

            if result.success and result.data and isinstance(result.data, str) and result.data.startswith("[KLEE_PENDING]"):
                task_text = result.data[len("[KLEE_PENDING]"):]
                if self._klee_delegate:
                    klee_reply = await self._klee_delegate(task_text)
                    result = ToolResult.ok(klee_reply)

            result_text = ""
            if result.success:
                result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
            else:
                result_text = f"错误: {result.error}"

            return tc["id"], result, result_text, display_name

        if len(tool_calls) > 1:
            exec_tasks = [_exec_one(tc) for tc in tool_calls]
            exec_results = await asyncio.gather(*exec_tasks, return_exceptions=True)
            for er in exec_results:
                if isinstance(er, Exception):
                    continue
                tcid, res, rtext, dname = er
                tool_results.append(res)
                tool_messages.append({"role": "tool", "tool_call_id": tcid, "content": rtext})
        else:
            for tc in tool_calls:
                t_name = tc["function"]["name"]
                t_args_str = tc["function"]["arguments"]
                display_name = TOOL_DISPLAY_NAMES.get(t_name, t_name)

                if self._tool_repair.detect_storm(t_name, t_args_str):
                    tool_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "该工具调用已被风暴检测拦截"})
                    continue

                repaired = self._tool_repair.repair_truncation(t_args_str)
                if repaired:
                    t_args_str = repaired

                try:
                    t_args = json.loads(t_args_str)
                except json.JSONDecodeError:
                    t_args = {}

                result = await self._tool_executor.execute(t_name, t_args, safe_mode=safe_mode)
                tool_results.append(result)

                if result.success and result.data and isinstance(result.data, str) and result.data.startswith("[KLEE_PENDING]"):
                    task_text = result.data[len("[KLEE_PENDING]"):]
                    if self._klee_delegate:
                        klee_reply = await self._klee_delegate(task_text)
                        result = ToolResult.ok(klee_reply)

                result_text = ""
                if result.success:
                    result_text = json.dumps(result.data, ensure_ascii=False) if not isinstance(result.data, str) else result.data
                else:
                    result_text = f"错误: {result.error}"

                tool_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text})

        messages.extend(tool_messages)

        final_reply = await self._summarize_results(current_user_input, tool_results, tool_calls, trace, user_openid=user_openid, session_id=session_id)
        rc = assistant_msg.get("reasoning_content", "")
        self._context.add_message("user", current_user_input, reasoning_content=rc if rc else None)
        self._context.add_message("assistant", final_reply)
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
                nahida_prompt = getattr(self._context, "system_prompt", "") or ""
                if not nahida_prompt and hasattr(self._context, "_system_prompt_loader") and self._context._system_prompt_loader:
                    try:
                        nahida_prompt = self._context._system_prompt_loader()
                    except Exception:
                        pass
            if not nahida_prompt:
                nahida_prompt = "你是纳西妲，须弥的草神。"

            summary_prompt = (
                "旅行者，工具查到结果了！你看看这些信息，帮人家整理一下回复给旅行者吧～\n\n"
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

        return combined
