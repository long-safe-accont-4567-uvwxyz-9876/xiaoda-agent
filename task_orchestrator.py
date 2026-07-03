import os
import re
import time
import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from openai import AsyncOpenAI

from loguru import logger
from agent_dispatcher import AgentDispatcher
from emotion.emoji_config import get_status_msg
from config import AGENT_ROUTE_KEYWORDS, DATA_DIR
from belief_router import BeliefRouter


class RouteCache:
    """LRU cache for routing decisions with TTL. Uses OrderedDict for O(1) operations."""

    def __init__(self, max_size: int = 200, ttl_seconds: float = 300.0) -> None:
        self._cache: OrderedDict[str, tuple[list[str], float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def get(self, user_input: str) -> list[str] | None:
        """Get cached route result. Returns None if not found or expired."""
        key = self._make_key(user_input)
        async with self._lock:
            if key not in self._cache:
                return None
            targets, ts = self._cache[key]
            if time.time() - ts > self._ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return targets

    async def put(self, user_input: str, targets: list[str]) -> None:
        """Cache a routing result."""
        key = self._make_key(user_input)
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (targets, time.time())
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def invalidate(self) -> None:
        """Clear all cached entries."""
        async with self._lock:
            self._cache.clear()

    async def invalidate_agent(self, agent_name: str) -> None:
        """Invalidate all cache entries that involve a specific agent."""
        async with self._lock:
            keys_to_remove = [
                k for k, (targets, _) in self._cache.items()
                if agent_name in targets
            ]
            for k in keys_to_remove:
                del self._cache[k]

    @staticmethod
    def _make_key(user_input: str) -> str:
        """Create a cache key from user input. Normalize whitespace and lowercase."""
        return " ".join(user_input.lower().split())


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
    skip_synthesis: bool = False
    # 子代理上下文（由调用方注入，传递给 dispatcher.dispatch 的 context 参数）
    sub_agent_context: str = ""

    def update(self, updates: dict) -> "TaskState":
        for k, v in updates.items():
            if hasattr(self, k):
                setattr(self, k, v)
        return self

    async def push_progress(self, msg: str) -> None:
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
    def __init__(self) -> None:
        self._nodes: dict[str, Callable] = {}
        self._edges: dict[str, Callable] = {}
        self._entry_point: str = ""
        self._compiled = False

    def add_node(self, name: str, handler: Callable[[TaskState], Awaitable[dict]]) -> None:
        self._nodes[name] = handler

    def add_conditional_edge(self, source: str, condition_fn: Callable[[TaskState], Awaitable[str]]) -> None:
        self._edges[source] = condition_fn

    def set_entry_point(self, name: str) -> None:
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
        max_node_visits = 2  # 同一节点访问超过此次数判环
        node_visit_count: dict[str, int] = {}
        global_deadline = time.monotonic() + 150  # 全局 150s 超时
        max_node_timeout = 120  # 单节点最大 120s（原 30s 会提前杀掉内层 180s 的 AgentNode）

        for step in range(max_steps):
            if current == END:
                break

            # 全局超时检查
            if time.monotonic() > global_deadline:
                logger.warning("task_graph.global_timeout", step=step)
                state.final_output = state.final_output or "任务执行超时"
                break

            # 环检测：同一节点访问次数超过阈值
            node_visit_count[current] = node_visit_count.get(current, 0) + 1
            if node_visit_count[current] > max_node_visits:
                logger.warning("task_graph.cycle_detected", node=current, visits=node_visit_count[current])
                state.final_output = state.final_output or f"检测到循环依赖，节点 {current} 被重复访问"
                break

            handler = self._nodes.get(current)
            if not handler:
                logger.warning("task_graph.node_not_found", node=current)
                break

            state.current_node = current
            logger.info("task_graph.executing", node=current, step=step)

            # 动态计算节点超时：取"单节点上限"和"全局剩余时间"的较小值，避免外层提前杀内层
            remaining = global_deadline - time.monotonic()
            node_timeout = min(max_node_timeout, remaining)
            try:
                updates = await asyncio.wait_for(handler(state), timeout=node_timeout)
                if updates:
                    state.update(updates)
            except asyncio.TimeoutError:
                logger.warning("task_graph.node_timeout", node=current, timeout=node_timeout)
                state.final_output = f"节点 {current} 执行超时（{node_timeout:.0f}s）"
                break
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
    _route_cache = RouteCache()  # class-level shared cache

    @staticmethod
    def _rule_route(user_input: str) -> list[str]:
        q = user_input.lower()
        search_kw = AGENT_ROUTE_KEYWORDS["xilian"]
        code_kw = AGENT_ROUTE_KEYWORDS["yinlang"]
        research_kw = AGENT_ROUTE_KEYWORDS["nike"]
        parallel_trigger_kw = AGENT_ROUTE_KEYWORDS["parallel_trigger"]
        nahida_only_patterns = AGENT_ROUTE_KEYWORDS["nahida"]

        # 否定上下文检测：用户明确说不要做某事时，不应路由到对应Agent
        is_negative = bool(re.search(
            r"(?:不|别|不要|不用|不需要|没必要)\s*(?:要|用|调用|查|检查|执行|运行|搜索|搜|找|看)",
            user_input
        )) or bool(re.search(
            r"(?:不需要|不用|别)\s*(?:调用|使用)\s*(?:这个|那个|任何)?\s*(?:工具|功能)",
            user_input
        ))
        if is_negative:
            return ["nahida"]

        matched = []
        if any(kw in q for kw in search_kw):
            matched.append("xilian")
        if any(kw in q for kw in code_kw):
            matched.append("yinlang")
        if any(kw in q for kw in research_kw):
            matched.append("nike")

        if any(kw in q for kw in nahida_only_patterns):
            return ["nahida"]

        is_parallel = any(kw in q for kw in parallel_trigger_kw)

        if len(matched) > 1 and is_parallel:
            return matched
        if len(matched) == 1:
            return matched
        return ["nahida"]

    @staticmethod
    def _has_mention(user_input: str) -> bool:
        """检测用户输入是否包含 @mention（高置信度路由信号）。

        复用 RouterEngine 的 MENTION_MAP，保持 @mention 路由的最高优先级与一致性。
        """
        from core.router_engine import MENTION_MAP
        return any(mention in user_input for mention in MENTION_MAP)

    @staticmethod
    def _detect_parallel_targets(user_input: str) -> list[tuple[str, str]] | None:
        """检测用户输入是否需要多子代理并行（无依赖）。

        匹配 "分别问 X 和 Y 回答..." / "让 X 和 Y 都分析..." 等显式并行请求，
        返回 [(name, input_text), ...] 或 None。name 可能是 display_name 或内部名，
        由 :meth:`_normalize_parallel_targets` 根据 agent_configs 归一化。
        """
        # 前缀 + 名称部分（贪婪到触发词前）+ 触发词 + 共同输入
        match = re.match(
            r"(?:分别问|让|请)\s*(.+?)(?:都\s*)?(?:回答|说|看看|分析|处理)\s*(.+)",
            user_input,
        )
        if not match:
            return None
        names_part = match.group(1)
        input_text = match.group(2) or user_input
        # 名称部分按 和 / , / ， / 、 拆分
        parts = re.split(r"[和,，、]", names_part)
        names = [p.strip() for p in parts if p.strip()]
        if len(names) < 2:
            return None
        return [(n, input_text) for n in names]

    @staticmethod
    def _normalize_parallel_targets(targets_inputs: list[tuple[str, str]],
                                     agent_configs: dict) -> list[str]:
        """将检测到的目标名归一化为 agent_configs 中的内部 name。

        同时支持内部名（如 ``keli``）与展示名（如 ``可莉``），忽略无效目标。
        """
        name_map: dict[str, str] = {}
        for name, cfg in agent_configs.items():
            name_map[name] = name
            name_map[name.lower()] = name
            if isinstance(cfg, dict):
                disp = cfg.get("display_name", "")
                if disp:
                    name_map[disp] = name
        valid: list[str] = []
        seen: set[str] = set()
        for raw_name, _ in targets_inputs:
            internal = name_map.get(raw_name) or name_map.get(raw_name.lower())
            if internal and internal not in seen:
                valid.append(internal)
                seen.add(internal)
        return valid

    def __init__(self, client: AsyncOpenAI, model: str = "mimo-v2.5", belief_router: BeliefRouter | None = None) -> None:
        self._client = client
        self._model = model
        self._belief_router = belief_router

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
        """路由入口：缓存 → 规则 → 信念/LLM 路由，返回路由结果 dict。"""
        user_input = state.user_input
        agent_configs = state._agent_configs

        if not agent_configs:
            return {"route_targets": ["nahida"], "route_target": "nahida", "route_plan": ["nahida"]}

        # SOLO 模式：任务→代理 1:1 绑定（参考 Trae SOLO 模式）
        # 在并行检测之前计算建议目标；仅在用户未明确指定子代理时使用
        suggested_target = None
        dispatcher = getattr(state, "_dispatcher", None)
        if dispatcher is not None:
            try:
                task_type = dispatcher.classify_task(user_input)
                suggested_target = dispatcher.route_task(task_type, user_input)
            except Exception as e:
                logger.warning("route.solo_routing_failed", error=str(e)[:200])

        # 0. 检测无依赖多子代理任务（如"分别问可莉和银狼..."）→ 直达并行路径
        parallel_targets = self._detect_parallel_targets(user_input)
        if parallel_targets and len(parallel_targets) >= 2:
            valid_targets = self._normalize_parallel_targets(parallel_targets, agent_configs)
            if len(valid_targets) >= 2:
                await self._route_cache.put(user_input, valid_targets)
                await self._notify_route_progress(state, valid_targets, agent_configs, "并行路由")
                return self._build_route_dict(valid_targets)

        # 若用户未明确指定子代理，使用 SOLO 建议的目标（仅当建议为具体子代理时）
        if (suggested_target
                and suggested_target != "keli"
                and suggested_target in agent_configs):
            targets = [suggested_target]
            await self._route_cache.put(user_input, targets)
            await self._notify_route_progress(state, targets, agent_configs, "SOLO路由")
            return self._build_route_dict(targets)

        # 1. 缓存命中
        cached = await self._route_cache.get(user_input)
        if cached is not None:
            logger.debug("route.cache_hit", input=user_input[:50], targets=cached)
            return self._build_route_dict(cached)

        # 2. 规则路由
        rule_result = self._rule_route(user_input)
        if rule_result:
            targets = [t for t in rule_result if t in agent_configs or t == "nahida"]
            if not targets:
                targets = ["nahida"]

            # 置信度评估：@mention 匹配高置信度（0.9），关键词正则匹配低置信度（0.5）
            _rule_confidence = 0.9 if self._has_mention(user_input) else 0.5

            # 低置信度触发 LLM 路由升级（LLM 准确率更高，优先于低置信度规则结果）
            if _rule_confidence < 0.6:
                try:
                    llm_targets = await self._llm_route_targets(user_input, agent_configs)
                    if llm_targets:
                        llm_valid = [t for t in llm_targets if t in agent_configs or t == "nahida"]
                        if llm_valid:
                            await self._route_cache.put(user_input, llm_valid)
                            await self._notify_route_progress(state, llm_valid, agent_configs, "LLM路由升级")
                            return self._build_route_dict(llm_valid)
                except Exception as e:
                    logger.warning("route.llm_upgrade_failed", error=str(e)[:200])

            await self._route_cache.put(user_input, targets)
            await self._notify_route_progress(state, targets, agent_configs, "路由分析")
            return self._build_route_dict(targets)

        # 3. 信念路由（Thompson Sampling，主 fallback）/ LLM 路由
        if self._belief_router:
            targets = [self._belief_router.select_agent(exclude={"nahida"})]
        else:
            targets = await self._llm_route_targets(user_input, agent_configs)

        # 4. 校验 + 返回
        valid_targets = [t for t in targets if t in agent_configs or t == "nahida"]
        if not valid_targets:
            valid_targets = ["nahida"]
        route_method = "信念路由" if self._belief_router else "LLM路由"
        await self._notify_route_progress(state, valid_targets, agent_configs, route_method)
        return self._build_route_dict(valid_targets)

    @staticmethod
    def _build_route_dict(targets: list[str]) -> dict:
        """根据 targets 列表构造统一的路由结果 dict。"""
        return {
            "route_targets": targets,
            "route_target": targets[0] if len(targets) == 1 else "",
            "route_plan": targets,
        }

    @staticmethod
    def _build_display_names(targets: list[str], agent_configs: dict) -> list[str]:
        """将 targets 映射为展示名（agent_configs 缺失则原样返回）。"""
        names = []
        for t in targets:
            if t in agent_configs:
                names.append(agent_configs[t].get("display_name", t))
            else:
                names.append(t)
        return names

    async def _notify_route_progress(self, state: TaskState, targets: list[str],
                                     agent_configs: dict, prefix: str) -> None:
        """路由成功后推送进度消息（仅 nahida 单路由时不打扰用户）。"""
        if len(targets) == 1 and targets[0] == "nahida":
            return
        display_names = self._build_display_names(targets, agent_configs)
        parallel = "并行处理" if len(targets) > 1 else ""
        await state.push_progress(f"🔀 {prefix}完成 → 交给{', '.join(display_names)}{parallel}")

    async def _llm_route_targets(self, user_input: str, agent_configs: dict) -> list[str]:
        """LLM 路由 fallback：调用 LLM 解析并归一化为 agent name 列表。"""
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

        # 构造 name → 规范 agent name 的映射表
        name_map = {}
        for n, cfg in agent_configs.items():
            name_map[n] = n
            name_map[cfg.get("display_name", "")] = n
        name_map["nahida"] = "nahida"
        name_map["纳西妲"] = "nahida"

        # 模糊匹配 LLM 输出的每个部分
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

        return targets if targets else ["nahida"]


class ParallelAgentNode:
    def __init__(self, dispatcher: AgentDispatcher, route_client: AsyncOpenAI, route_model: str = "mimo-v2.5", belief_router: BeliefRouter | None = None) -> None:
        self._dispatcher = dispatcher
        self._route_client = route_client
        self._route_model = route_model
        self._belief_router = belief_router
        self._agent_configs: dict = {}

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

    async def _decompose_task_v2(self, user_input: str, targets: list[str]) -> dict[str, str]:
        """用 LLM 做智能任务拆分，返回 {agent_name: task_description}。

        - 单一 Agent 场景直接短路，不调用 LLM。
        - LLM 调用或 JSON 解析失败时 fallback 到原 _decompose_task 逻辑。
        """
        # 单一 Agent 场景：直接返回，不调用 LLM
        if len(targets) == 1:
            return {targets[0]: user_input}

        agent_configs = self._agent_configs

        # 构建 prompt
        prompt = self._build_decompose_v2_prompt(user_input, targets, agent_configs)

        try:
            # LLM 调用（含 response_format 降级）
            content = await self._call_decompose_llm(prompt)

            # JSON 解析与校验
            return self._parse_and_validate_decompose_result(content, targets)

        except Exception as e:
            logger.warning("decompose_task_v2.fallback", error=str(e))
            # fallback 到原 _decompose_task 逻辑
            return await self._decompose_task(user_input, targets, agent_configs)

    def _build_decompose_v2_prompt(self, user_input: str, targets: list[str],
                                     agent_configs: dict) -> str:
        """构建任务分解 LLM 的 prompt（含 agent 描述列表与输出格式要求）。"""
        # 构建 agent 描述列表
        agent_descriptions = []
        for t in targets:
            if t in agent_configs:
                cfg = agent_configs[t]
                display_name = cfg.get("display_name", t)
                desc = cfg.get("route_description", "")
                caps = cfg.get("capabilities", [])
                caps_str = ", ".join(caps) if caps else "综合分析"
                agent_descriptions.append(
                    f"- {t}（{display_name}）：专长 [{desc}]，能力 [{caps_str}]"
                )
            else:
                agent_descriptions.append(f"- {t}：通用Agent")

        agents_block = "\n".join(agent_descriptions)

        prompt = f"""你是一个智能任务分解器。需要将用户的复杂请求拆分为给不同Agent的子任务。

用户原始请求：
{user_input}

可用 Agent 及其专长：
{agents_block}

请为每个 Agent 生成一个针对性的子任务描述。要求：
1. 每个子任务必须聚焦于该 Agent 擅长的领域，充分利用其专长
2. 子任务之间不应有重复的工作，职责边界清晰
3. 子任务要具体、可执行，包含明确的输入上下文和期望输出格式
4. 如果存在依赖关系，请在任务描述中标注（例如"等待xxx的结果后再执行"）
5. 保持原问题的核心意图，不要遗漏关键信息
6. 必须为上述每一个 Agent 都分配子任务

请严格按以下 JSON 格式输出，不要输出任何其他内容：
{{"agent_name": "针对该Agent的具体子任务描述"}}

其中 agent_name 必须是上面列出的 Agent 名称之一。"""
        return prompt

    async def _call_decompose_llm(self, prompt: str) -> str:
        """调用任务分解 LLM（response_format 不支持时降级为普通调用），返回响应文本。"""
        # response_format 仅部分模型支持，不支持时降级为普通调用
        try:
            response = await self._route_client.chat.completions.create(
                model=self._route_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.3,
            )
        except Exception:
            response = await self._route_client.chat.completions.create(
                model=self._route_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )

        content = (response.choices[0].message.content or "").strip()
        return content

    def _parse_and_validate_decompose_result(self, content: str,
                                               targets: list[str]) -> dict[str, str]:
        """解析 LLM 返回的 JSON 并校验每个 target 都有非空子任务。返回 sub_tasks 字典。"""
        # 解析 JSON：先直接解析，失败则尝试从文本中提取
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', content)
            if not match:
                raise ValueError("LLM 响应中未找到 JSON")
            result = json.loads(match.group(0))

        # 校验：确保每个 target 都有非空任务
        sub_tasks: dict[str, str] = {}
        for t in targets:
            task_desc = result.get(t)
            if isinstance(task_desc, str) and task_desc.strip():
                sub_tasks[t] = task_desc
            else:
                raise ValueError(f"Agent {t} 缺失子任务或任务为空")

        if len(sub_tasks) != len(targets):
            raise ValueError("子任务数量与目标 Agent 数量不匹配")

        return sub_tasks

    async def execute_single(self, target: str, task_prompt: str, state: TaskState) -> dict | None:
        agent = self._dispatcher.get_agent(target)
        if not agent or not agent.available:
            await RouterNode._route_cache.invalidate_agent(target)
            if self._belief_router:
                self._belief_router.update_belief(target, False)
            return {"agent": target, "display_name": target, "reply": f"{target}暂时不可用", "error": True}

        display_name = agent.config.display_name
        try:
            # 传递子代理上下文（由调用方通过 state.sub_agent_context 注入）
            _context = state.sub_agent_context or None
            reply = await asyncio.wait_for(
                self._dispatcher.dispatch(target, task_prompt, context=_context, status_callback=None),
                timeout=180,
            )
            if reply is None:
                reply = f"{display_name}现在有点累了...等会儿再来吧！💤"
                if self._belief_router:
                    self._belief_router.update_belief(target, False)
            else:
                if self._belief_router:
                    self._belief_router.update_belief(target, True)
            return {"agent": target, "display_name": display_name, "reply": reply}
        except asyncio.TimeoutError:
            if self._belief_router:
                self._belief_router.update_belief(target, False)
            return {"agent": target, "display_name": display_name, "reply": f"{display_name}处理超时", "error": True}
        except Exception as e:
            if self._belief_router:
                self._belief_router.update_belief(target, False)
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

        # 将 agent_configs 暴露到 self，供 _decompose_task_v2 使用
        self._agent_configs = state._agent_configs
        sub_tasks = await self._decompose_task_v2(state.user_input, targets)

        for t in targets:
            display_name = t
            if t in state._agent_configs:
                display_name = state._agent_configs[t].get("display_name", t)
            # await state.push_progress(get_status_msg(t, "thinking", f"{display_name}准备就绪...", None))  # 节流：并行模式下由外层统一汇报

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
                emoji = "✅" if not r.get("error") else "❌"
                # await state.push_progress(f"{emoji} {display_name}已完成 ({status})")  # 节流：并行模式下由外层统一汇报
                intermediate.append(r)

        all_replies = "\n\n".join([f"【{r['display_name']}】\n{r['reply']}" for r in intermediate])
        await state.push_progress(f"🎯 全部{len(targets)}个Agent已执行完毕，进入结果综合...")

        return {"sub_agent_reply": all_replies, "intermediate_results": intermediate}


class AgentNode:
    def __init__(self, dispatcher: AgentDispatcher, belief_router: BeliefRouter | None = None) -> None:
        self._dispatcher = dispatcher
        self._belief_router = belief_router

    async def execute(self, state: TaskState) -> dict:
        target = state.route_target
        if not target or target == "nahida":
            return {"final_output": "", "sub_agent_reply": ""}

        agent = self._dispatcher.get_agent(target)
        if not agent or not agent.available:
            await state.push_progress(f"⚠️ {target}暂时不可用")
            if self._belief_router:
                self._belief_router.update_belief(target, False)
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
                if self._belief_router:
                    self._belief_router.update_belief(target, False)
            else:
                if self._belief_router:
                    self._belief_router.update_belief(target, True)

            await state.push_progress(get_status_msg(target, "done", f"{display_name}已完成！", agent.config.personality_file))

            result_entry = {"agent": target, "display_name": display_name, "reply": reply}
            intermediate = list(state.intermediate_results)
            intermediate.append(result_entry)

            # 单Agent时直接输出，跳过SynthesisNode
            return {"sub_agent_reply": reply, "intermediate_results": intermediate, "final_output": reply, "skip_synthesis": True}

        except asyncio.TimeoutError:
            logger.warning("agent_node.timeout", target=target)
            await state.push_progress(f"⏰ {display_name}处理超时")
            if self._belief_router:
                self._belief_router.update_belief(target, False)
            return {"sub_agent_reply": f"{display_name}处理超时，请稍后再试"}
        except Exception as e:
            logger.error("agent_node.execute_failed", target=target, error=str(e))
            await state.push_progress(f"❌ {display_name}处理失败")
            if self._belief_router:
                self._belief_router.update_belief(target, False)
            return {"sub_agent_reply": f"处理出错: {e}"}


class SynthesisNode:
    def __init__(self, client: AsyncOpenAI, model: str = "mimo-v2.5", nahida_chat_callback: Optional[Any]=None) -> None:
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

        # nahida_chat不可用时，使用LLM综合作为后备
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
    if getattr(state, 'skip_synthesis', False):
        return END
    if len(targets) > 1:
        return PARALLEL_EXECUTE
    return SINGLE_EXECUTE


def build_task_graph(dispatcher: AgentDispatcher, agent_configs: dict,
                     route_client: AsyncOpenAI, route_model: str = "mimo-v2.5",
                     nahida_chat_callback: Optional[Any]=None) -> TaskGraph:
    db_path = str(DATA_DIR / "agent.db")
    belief_router = BeliefRouter(db_path=db_path)
    router = RouterNode(route_client, route_model, belief_router=belief_router)
    parallel_node = ParallelAgentNode(dispatcher, route_client, route_model, belief_router=belief_router)
    agent_node = AgentNode(dispatcher, belief_router=belief_router)
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
    graph.add_conditional_edge(SINGLE_EXECUTE, lambda s: END if getattr(s, 'skip_synthesis', False) else "synthesis")
    graph.add_conditional_edge("synthesis", lambda s: END)

    graph.compile()

    graph._agent_configs = agent_configs
    graph._dispatcher = dispatcher
    graph._router = router
    graph._route_client = route_client
    graph._route_model = route_model
    graph._belief_router = belief_router

    return graph


async def run_task_graph(graph: TaskGraph, user_input: str, user_id: str,
                         session_id: str = "", status_callback: Optional[Any]=None,
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
