import os
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from openai import AsyncOpenAI

from loguru import logger
from agent_dispatcher import AgentDispatcher
from emoji_config import get_status_msg


@dataclass
class TaskState:
    user_input: str
    user_id: str
    session_id: str = ""
    current_node: str = ""
    route_target: str = ""
    route_targets: list[str] = field(default_factory=list)
    route_plan: list[str] = field(default_factory=list)
    current_step_index: int = 0
    sub_agent_reply: str = ""
    intermediate_results: list[dict] = field(default_factory=list)
    final_output: str = ""
    progress_log: list[str] = field(default_factory=list)
    status_callback: Any = None
    _dispatcher: Any = None
    _agent_configs: dict = field(default_factory=dict)

    def update(self, updates: dict) -> "TaskState":
        for k, v in updates.items():
            if hasattr(self, k):
                setattr(self, k, v)
        return self

    async def push_progress(self, msg: str):
        self.progress_log.append(msg)
        if self.status_callback:
            try:
                await self.status_callback(msg)
            except Exception:
                pass


END = "__END__"
PARALLEL_EXECUTE = "__parallel_execute__"
SINGLE_EXECUTE = "__single_execute__"


class TaskGraph:
    def __init__(self):
        self._nodes: dict[str, Callable] = {}
        self._edges: dict[str, Callable] = {}
        self._entry_point: str = ""
        self._compiled = False

    def add_node(self, name: str, handler: Callable[[TaskState], Awaitable[dict]]):
        self._nodes[name] = handler

    def add_conditional_edge(self, source: str, condition_fn: Callable[[TaskState], Awaitable[str]]):
        self._edges[source] = condition_fn

    def set_entry_point(self, name: str):
        self._entry_point = name

    def compile(self) -> "TaskGraph":
        if not self._entry_point:
            raise ValueError("Entry point not set")
        if self._entry_point not in self._nodes:
            raise ValueError(f"Entry point node '{self._entry_point}' not found")
        self._compiled = True
        return self

    async def run(self, initial_state: TaskState) -> TaskState:
        if not self._compiled:
            raise RuntimeError("Graph not compiled. Call compile() first.")

        state = initial_state
        current = self._entry_point
        max_steps = 15

        for step in range(max_steps):
            if current == END:
                break

            handler = self._nodes.get(current)
            if not handler:
                logger.warning("task_graph.node_not_found", node=current)
                break

            state.current_node = current
            logger.info("task_graph.executing", node=current, step=step)

            try:
                updates = await handler(state)
                if updates:
                    state.update(updates)
            except Exception as e:
                logger.error("task_graph.node_error", node=current, error=str(e))
                state.final_output = f"任务执行出错: {e}"
                break

            edge_fn = self._edges.get(current)
            if edge_fn:
                try:
                    result = edge_fn(state)
                    if asyncio.iscoroutine(result):
                        next_node = await result
                    else:
                        next_node = result
                    current = next_node
                except Exception as e:
                    logger.error("task_graph.edge_error", node=current, error=str(e))
                    break
            else:
                break

        return state


class RouterNode:
    @staticmethod
    def _rule_route(user_input: str) -> list[str]:
        q = user_input.lower()
        search_kw = ["搜索", "搜一下", "查一下", "找一下", "帮我查", "帮我搜", "搜索一下",
                     "查资料", "最新", "新闻", "资讯", "获取网上", "看看有没有"]
        code_kw = ["代码", "编程", "写代码", "debug", "调试", "程序", "开发", "部署",
                   "git", "api", "接口", "函数", "脚本", "运行", "执行命令",
                   "巡检", "检查系统", "磁盘", "内存", "cpu", "进程", "服务状态",
                   "日志", "监控", "系统信息", "香橙派", "orange pi", "服务器",
                   "docker", "容器", "网络", "端口", "防火墙", "配置文件",
                   "gpio", "i2c", "spi", "传感器", "led", "舵机", "硬件", "引脚",
                   "串口", "uart", "pwm", "adc", "dac",
                   "摄像头", "拍照", "看看", "观察", "识别", "检测",
                   "重启服务", "部署", "服务状态", "系统服务",
                   "重启", "服务",
                   ]
        research_kw = ["研究", "分析", "学术", "论文", "深度", "计算复杂度", "数学证明",
                       "物理", "化学", "生物", "统计", "推导", "公式"]
        parallel_trigger_kw = [
            "全面", "整体", "综合", "各个方面", "多方面", "同时",
            "全部", "一起", "都检查", "都搜一下", "分别",
            "全方位", "彻底", "完整", "所有", "各个板块",
            "巡检", "体检", "诊断", "健康检查", "状况报告",
        ]

        matched = []
        if any(kw in q for kw in search_kw):
            matched.append("xilian")
        if any(kw in q for kw in code_kw):
            matched.append("yinlang")
        if any(kw in q for kw in research_kw):
            matched.append("nike")

        nahida_only_patterns = [
            "天气", "气温", "温度", "下雨", "晴天", "阴天",
            "时间", "几点", "现在几点", "日期", "今天星期几",
            "翻译", "意思是什么",
        ]
        if any(kw in q for kw in nahida_only_patterns):
            return ["nahida"]

        is_parallel = any(kw in q for kw in parallel_trigger_kw)

        if len(matched) > 1 and is_parallel:
            return matched
        if len(matched) == 1:
            return matched
        return ["nahida"]

    def __init__(self, client: AsyncOpenAI, model: str = "mimo-v2.5"):
        self._client = client
        self._model = model

    def _build_route_prompt(self, user_input: str, agent_configs: dict) -> str:
        agent_list = []
        for name, cfg in agent_configs.items():
            if name == "keli":
                continue
            caps = ", ".join(cfg.get("capabilities", []))
            desc = cfg.get("route_description", "")
            agent_list.append(f"- {name}（{cfg.get('display_name', name)}）: 能力[{caps}] {desc}")
        agent_list.append("- nahida（纳西妲）: 能力[chat, emotion, daily, general] 日常对话、情感交流、综合分析")

        return f"""你是一个任务路由器。根据用户输入，决定应该由哪些Agent来处理。

可用Agent列表:
{chr(10).join(agent_list)}

规则:
1. 返回Agent的name字段值，多个Agent用逗号分隔（如：yinlang,xilian）
2. 编程/代码/技术问题 → yinlang
3. 搜索/查询/探索/发现信息 → xilian
4. 研究/分析/学术/深度思考 → nike
5. 如果用户要求全面/综合/同时处理多个方面，可以返回多个Agent名称，用逗号分隔
6. 日常闲聊/情感/综合问题 → nahida
7. 如果不确定，返回 nahida
8. 最多返回3个Agent

用户输入: {user_input}

请只返回Agent名称（多个用逗号分隔）:"""

    async def route(self, state: TaskState) -> dict:
        user_input = state.user_input
        agent_configs = state._agent_configs

        if not agent_configs:
            return {"route_targets": ["nahida"], "route_target": "nahida", "route_plan": ["nahida"]}

        rule_result = self._rule_route(user_input)
        if rule_result:
            targets = [t for t in rule_result if t in agent_configs or t == "nahida"]
            if not targets:
                targets = ["nahida"]
            display_names = []
            for t in targets:
                if t in agent_configs:
                    display_names.append(agent_configs[t].get("display_name", t))
                else:
                    display_names.append(t)
            if len(targets) == 1 and targets[0] == "nahida":
                pass
            else:
                await state.push_progress(f"🔀 路由分析完成 → 交给{', '.join(display_names)}{'并行处理' if len(targets) > 1 else ''}")
            return {
                "route_targets": targets,
                "route_target": targets[0] if len(targets) == 1 else "",
                "route_plan": targets,
            }

        prompt = self._build_route_prompt(user_input, agent_configs)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30,
            temperature=0.1,
        )
        msg = response.choices[0].message
        raw_result = msg.content.strip() if msg.content else ""

        if not raw_result:
            rc = getattr(msg, "reasoning_content", None) or ""
            raw_result = rc[:50] if rc else ""

        name_map = {}
        for n, cfg in agent_configs.items():
            name_map[n] = n
            name_map[cfg.get("display_name", "")] = n
        name_map["nahida"] = "nahida"
        name_map["纳西妲"] = "nahida"

        targets = []
        seen = set()
        for part in raw_result.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            matched = name_map.get(part)
            if not matched:
                for key, val in name_map.items():
                    if key and key in part and val not in seen:
                        matched = val
                        break
            if matched and matched not in seen:
                targets.append(matched)
                seen.add(matched)

        if not targets:
            targets = ["nahida"]

        valid_targets = [t for t in targets if t in agent_configs or t == "nahida"]
        if not valid_targets:
            valid_targets = ["nahida"]

        display_names = []
        for t in valid_targets:
            if t in agent_configs:
                display_names.append(agent_configs[t].get("display_name", t))
            else:
                display_names.append(t)

        if not (len(valid_targets) == 1 and valid_targets[0] == "nahida"):
            await state.push_progress(f"🔀 LLM路由分析完成 → 交给{', '.join(display_names)}{'并行处理' if len(valid_targets) > 1 else ''}")

        return {
            "route_targets": valid_targets,
            "route_target": valid_targets[0] if len(valid_targets) == 1 else "",
            "route_plan": valid_targets,
        }


class ParallelAgentNode:
    def __init__(self, dispatcher: AgentDispatcher, route_client: AsyncOpenAI, route_model: str = "mimo-v2.5"):
        self._dispatcher = dispatcher
        self._route_client = route_client
        self._route_model = route_model

    def _build_decompose_prompt(self, user_input: str, targets: list[str], agent_configs: dict) -> str:
        target_descs = []
        for t in targets:
            if t in agent_configs:
                cfg = agent_configs[t]
                caps = ", ".join(cfg.get("capabilities", []))
                target_descs.append(f"- {t}（{cfg.get('display_name', t)}）：擅长 [{caps}]")
            else:
                target_descs.append(f"- {t}")

        return f"""你需要将用户的请求拆分为给不同Agent的子任务。

用户原始请求: {user_input}

需要分配给的Agent:
{chr(10).join(target_descs)}

请为每个Agent生成一个针对性的子任务描述。要求：
1. 每个子任务应该聚焦于该Agent擅长的领域
2. 子任务之间不应该有重复的工作
3. 每个子任务要具体、可执行
4. 保持原问题的核心意图

请严格按以下JSON格式输出，不要输出其他内容：
{{"子任务": {{"agent_name": "针对该Agent的具体子任务描述"}}}}"""
    async def _decompose_task(self, user_input: str, targets: list[str], agent_configs: dict) -> dict[str, str]:
        sub_tasks = {}
        capabilities_map = {}
        for t in targets:
            if t in agent_configs:
                cfg = agent_configs[t]
                capabilities_map[t] = cfg.get("capabilities", [])
                capabilities_map[t + "_desc"] = cfg.get("route_description", "")

        if len(targets) == 1:
            sub_tasks[targets[0]] = user_input
            return sub_tasks

        if len(targets) == 2:
            t0, t1 = targets[0], targets[1]
            cap0 = capabilities_map.get(t0, [])
            cap1 = capabilities_map.get(t1, [])
            desc0 = capabilities_map.get(t0 + "_desc", "")
            desc1 = capabilities_map.get(t1 + "_desc", "")

            sub_tasks[t0] = (
                f"用户请求：{user_input}\n\n"
                f"你的专长领域：{desc0 or t0}\n"
                f"你的能力：{', '.join(cap0) if cap0 else '综合分析'}\n\n"
                f"请针对上述用户请求，**仅从你擅长的{desc0 or t0}角度**给出具体的分析、结论或行动方案。"
                f"聚焦核心问题，输出实质性内容，不要泛泛而谈。"
            )
            sub_tasks[t1] = (
                f"用户请求：{user_input}\n\n"
                f"你的专长领域：{desc1 or t1}\n"
                f"你的能力：{', '.join(cap1) if cap1 else '综合分析'}\n\n"
                f"请针对上述用户请求，**仅从你擅长的{desc1 or t1}角度**给出具体的分析、结论或行动方案。"
                f"聚焦核心问题，输出实质性内容，不要泛泛而谈。"
            )
            return sub_tasks

        for i, t in enumerate(targets):
            desc = capabilities_map.get(t + "_desc", t)
            caps = capabilities_map.get(t, [])
            sub_tasks[t] = (
                f"【任务{i+1}/{len(targets)}】用户请求：{user_input}\n\n"
                f"你的专长领域：{desc}\n"
                f"你的能力：{', '.join(caps) if caps else '综合分析'}\n\n"
                f"请**严格限定在你擅长的{desc}领域内**，针对该请求给出专业、具体的分析和结论。"
                f"只输出与你领域直接相关的内容，忽略其他方面。"
            )

        return sub_tasks

    async def execute_single(self, target: str, task_prompt: str, state: TaskState) -> dict | None:
        agent = self._dispatcher.get_agent(target)
        if not agent or not agent.available:
            return {"agent": target, "display_name": target, "reply": f"{target}暂时不可用", "error": True}

        display_name = agent.config.display_name
        try:
            reply = await asyncio.wait_for(
                self._dispatcher.dispatch(target, task_prompt, status_callback=None),
                timeout=180,
            )
            if reply is None:
                reply = f"{display_name}现在有点累了...等会儿再来吧！💤"
            return {"agent": target, "display_name": display_name, "reply": reply}
        except asyncio.TimeoutError:
            return {"agent": target, "display_name": display_name, "reply": f"{display_name}处理超时", "error": True}
        except Exception as e:
            return {"agent": target, "display_name": display_name, "reply": f"{display_name}处理出错: {e}", "error": True}

    async def execute(self, state: TaskState) -> dict:
        targets = state.route_targets
        if not targets:
            return {"sub_agent_reply": "", "final_output": ""}

        if len(targets) == 1:
            target = targets[0]
            if target == "nahida":
                return {"final_output": "", "sub_agent_reply": ""}
            single_result = await self.execute_single(target, state.user_input, state)
            if single_result:
                return {"sub_agent_reply": single_result.get("reply", ""), "intermediate_results": [single_result]}
            return {"sub_agent_reply": "", "intermediate_results": []}

        await state.push_progress(f"⚡ 启动并行模式，同时调度 {len(targets)} 个Agent...")

        sub_tasks = await self._decompose_task(state.user_input, targets, state._agent_configs)

        for t in targets:
            display_name = t
            if t in state._agent_configs:
                display_name = state._agent_configs[t].get("display_name", t)

        tasks = [
            self.execute_single(t, sub_tasks.get(t, state.user_input), state)
            for t in targets
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        intermediate = []
        for r in results:
            if isinstance(r, Exception):
                logger.error("parallel_agent.exception", error=str(r))
                intermediate.append({"agent": "unknown", "display_name": "未知", "reply": f"执行异常: {r}", "error": True})
            elif isinstance(r, dict):
                display_name = r.get("display_name", r.get("agent", ""))
                status = "done" if not r.get("error") else "error"
                intermediate.append(r)

        all_replies = "\n\n".join([f"【{r['display_name']}】\n{r['reply']}" for r in intermediate])
        await state.push_progress(f"🎯 全部{len(targets)}个Agent已执行完毕，进入结果综合...")

        return {"sub_agent_reply": all_replies, "intermediate_results": intermediate}


class AgentNode:
    def __init__(self, dispatcher: AgentDispatcher):
        self._dispatcher = dispatcher

    async def execute(self, state: TaskState) -> dict:
        target = state.route_target
        if not target or target == "nahida":
            return {"final_output": "", "sub_agent_reply": ""}

        agent = self._dispatcher.get_agent(target)
        if not agent or not agent.available:
            await state.push_progress(f"⚠️ {target}暂时不可用")
            return {"sub_agent_reply": f"该Agent暂时不可用", "final_output": ""}

        display_name = agent.config.display_name
        await state.push_progress(get_status_msg(target, "using", f"{display_name}正在处理...", agent.config.personality_file))

        try:
            reply = await asyncio.wait_for(
                self._dispatcher.dispatch(target, state.user_input, status_callback=None),
                timeout=180,
            )
            if reply is None:
                reply = f"{display_name}现在有点累了...等会儿再来吧！💤"

            await state.push_progress(get_status_msg(target, "done", f"{display_name}已完成！", agent.config.personality_file))

            result_entry = {"agent": target, "display_name": display_name, "reply": reply}
            intermediate = list(state.intermediate_results)
            intermediate.append(result_entry)

            return {"sub_agent_reply": reply, "intermediate_results": intermediate}

        except asyncio.TimeoutError:
            logger.warning("agent_node.timeout", target=target)
            await state.push_progress(f"⏰ {display_name}处理超时")
            return {"sub_agent_reply": f"{display_name}处理超时，请稍后再试"}
        except Exception as e:
            logger.error("agent_node.execute_failed", target=target, error=str(e))
            await state.push_progress(f"❌ {display_name}处理失败")
            return {"sub_agent_reply": f"处理出错: {e}"}


class SynthesisNode:
    def __init__(self, client: AsyncOpenAI, model: str = "mimo-v2.5", nahida_chat_callback=None):
        self._client = client
        self._model = model
        self._nahida_chat = nahida_chat_callback

    async def synthesize(self, state: TaskState) -> dict:
        results = state.intermediate_results
        if not results:
            return {"final_output": state.sub_agent_reply}

        await state.push_progress(get_status_msg("nahida", "done", "纳西妲正在整理全部结果...", None))

        parts = []
        for r in results:
            parts.append(f"【{r['display_name']}的回复】\n{r['reply']}")
        combined = "\n\n".join(parts)

        if self._nahida_chat:
            try:
                agent_count = len(results)
                agent_names = "、".join([r['display_name'] for r in results])
                prompt = f"""以下是{agent_count}位团队成员（{agent_names}）的并行工作结果，请你整理后向用户做一份完整的汇报：

{combined}

要求：
- 先给出一个总体概述（一句话总结全局情况）
- 然后按每个团队成员分板块汇报，提取所有具体的事实、数据、标题和关键信息
- 最后给出一个综合评估或建议
- 用清晰的结构组织，先总述再分点
- 不要只说空洞的感想或比喻，必须有实际信息量
- 语气温柔但内容必须充实
- 如果某个Agent的结果明显不完整或报错，如实说明"""
                final = await self._nahida_chat(prompt)
                return {"final_output": final}
            except Exception as e:
                logger.warning("synthesis.nahida_failed", error=str(e))

        if len(results) == 1:
            return {"final_output": results[0].get("reply", state.sub_agent_reply)}

        try:
            prompt = f"""请将以下{len(results)}个Agent的并行工作结果整理成清晰的汇报，提取所有具体信息：

{combined}

要求：
- 列出具体的事实、数据、标题
- 先一句话总述，再按Agent分点列出关键信息
- 不要空洞的比喻，必须有实际内容"""
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.5,
            )
            final = response.choices[0].message.content.strip()
            return {"final_output": final}
        except Exception as e:
            logger.warning("synthesis.fallback_failed", error=str(e))
            return {"final_output": combined}


async def route_condition(state: TaskState) -> str:
    targets = state.route_targets
    if not targets or (len(targets) == 1 and targets[0] == "nahida"):
        return END
    if len(targets) > 1:
        return PARALLEL_EXECUTE
    return SINGLE_EXECUTE


def build_task_graph(dispatcher: AgentDispatcher, agent_configs: dict,
                     route_client: AsyncOpenAI, route_model: str = "mimo-v2.5",
                     nahida_chat_callback=None) -> TaskGraph:
    router = RouterNode(route_client, route_model)
    parallel_node = ParallelAgentNode(dispatcher, route_client, route_model)
    agent_node = AgentNode(dispatcher)
    synthesis = SynthesisNode(route_client, route_model, nahida_chat_callback=nahida_chat_callback)

    graph = TaskGraph()

    async def router_handler(state: TaskState) -> dict:
        return await router.route(state)

    async def parallel_handler(state: TaskState) -> dict:
        return await parallel_node.execute(state)

    async def agent_handler(state: TaskState) -> dict:
        return await agent_node.execute(state)

    async def synthesis_handler(state: TaskState) -> dict:
        return await synthesis.synthesize(state)

    graph.add_node("router", router_handler)
    graph.add_node(PARALLEL_EXECUTE, parallel_handler)
    graph.add_node(SINGLE_EXECUTE, agent_handler)
    graph.add_node("synthesis", synthesis_handler)

    graph.set_entry_point("router")

    graph.add_conditional_edge("router", route_condition)
    graph.add_conditional_edge(PARALLEL_EXECUTE, lambda s: "synthesis")
    graph.add_conditional_edge(SINGLE_EXECUTE, lambda s: "synthesis")
    graph.add_conditional_edge("synthesis", lambda s: END)

    graph.compile()

    graph._agent_configs = agent_configs
    graph._dispatcher = dispatcher
    graph._router = router
    graph._route_client = route_client
    graph._route_model = route_model

    return graph


async def run_task_graph(graph: TaskGraph, user_input: str, user_id: str,
                         session_id: str = "", status_callback=None,
                         agent_configs: dict = None,
                         dispatcher: AgentDispatcher = None) -> TaskState:
    state = TaskState(
        user_input=user_input,
        user_id=user_id,
        session_id=session_id,
        status_callback=status_callback,
        _dispatcher=dispatcher,
        _agent_configs=agent_configs or {},
    )
    result = await graph.run(state)
    return result