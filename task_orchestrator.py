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
                break
            state.current_node = current
            try:
                updates = await handler(state)
                if updates:
                    state.update(updates)
            except Exception as e:
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
                except Exception:
                    break
            else:
                break
        return state


class RouterNode:
    @staticmethod
    def _rule_route(user_input: str) -> list[str]:
        q = user_input.lower()
        search_kw = ["搜索", "搜一下", "查一下", "找一下", "帮我查", "帮我搜"]
        code_kw = ["代码", "编程", "写代码", "debug", "调试", "程序", "开发", "部署"]
        research_kw = ["研究", "分析", "学术", "论文", "深度"]

        matched = []
        if any(kw in q for kw in search_kw):
            matched.append("xilian")
        if any(kw in q for kw in code_kw):
            matched.append("yinlang")
        if any(kw in q for kw in research_kw):
            matched.append("nike")

        if len(matched) == 1:
            return matched
        if len(matched) > 1:
            return matched
        return ["nahida"]

    def __init__(self, client: AsyncOpenAI, model: str = "mimo-v2.5"):
        self._client = client
        self._model = model

    async def route(self, state: TaskState) -> dict:
        user_input = state.user_input
        agent_configs = state._agent_configs

        rule_result = self._rule_route(user_input)
        if rule_result:
            targets = [t for t in rule_result if t in agent_configs or t == "nahida"]
            if not targets:
                targets = ["nahida"]
            return {
                "route_targets": targets,
                "route_target": targets[0] if len(targets) == 1 else "",
                "route_plan": targets,
            }
        return {"route_targets": ["nahida"], "route_target": "nahida", "route_plan": ["nahida"]}


class ParallelAgentNode:
    def __init__(self, dispatcher: AgentDispatcher, route_client: AsyncOpenAI, route_model: str = "mimo-v2.5"):
        self._dispatcher = dispatcher
        self._route_client = route_client
        self._route_model = route_model

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
                reply = f"{display_name}现在有点累了..."
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

        tasks = [self.execute_single(t, state.user_input, state) for t in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        intermediate = []
        for r in results:
            if isinstance(r, Exception):
                intermediate.append({"agent": "unknown", "display_name": "未知", "reply": f"执行异常: {r}", "error": True})
            elif isinstance(r, dict):
                intermediate.append(r)
        all_replies = "\n\n".join([f"【{r['display_name']}】\n{r['reply']}" for r in intermediate])
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
            return {"sub_agent_reply": f"该Agent暂时不可用", "final_output": ""}
        display_name = agent.config.display_name
        try:
            reply = await asyncio.wait_for(
                self._dispatcher.dispatch(target, state.user_input, status_callback=None),
                timeout=180,
            )
            if reply is None:
                reply = f"{display_name}现在有点累了..."
            result_entry = {"agent": target, "display_name": display_name, "reply": reply}
            intermediate = list(state.intermediate_results)
            intermediate.append(result_entry)
            return {"sub_agent_reply": reply, "intermediate_results": intermediate}
        except asyncio.TimeoutError:
            return {"sub_agent_reply": f"{display_name}处理超时"}
        except Exception as e:
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
        if len(results) == 1:
            return {"final_output": results[0].get("reply", state.sub_agent_reply)}
        parts = [f"【{r['display_name']}的回复】\n{r['reply']}" for r in results]
        combined = "\n\n".join(parts)
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
    return graph
