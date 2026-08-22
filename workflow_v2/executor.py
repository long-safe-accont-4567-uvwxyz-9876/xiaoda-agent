# workflow_v2/executor.py
"""M1 统一执行器：按节点类型把执行交给现有能力层（决策 2026-08-22 立项）。

转正立项 §3：TOOL/MCP 走 ``ToolExecutor.execute``（复用注册表/沙箱/权限/
审批——approver 由主进程注入，与主对话卡片确认一致；无 approver 时以默认
deny 兜底，杜绝"静默执行写操作"），MODEL/SKILL/LEGACY_PROMPT 走
ModelRouter，SKILL 节点由独立解析器取技能内容后再独立推理（决策 B）。

设计约束：
- 永不静默：未知/未实现节点返回显式 UNSUPPORTED_NODE 失败，落库可追踪；
  - 安全横切：节点参数支持 ``{{secret:name}}`` 占位符（运行时解析，不落库
    不入日志）；有 SecurityFilter 时对文本参数先做 check_user_input（action=
    block 才拒绝，warn 放行与主对话一致），LLM 输出再过 check_output_privacy
    防隐私回显；
- 超时/重试：节点级 timeout_seconds（asyncio.wait_for）+ retry_policy
  （max_attempts 指数退避）；
- 依赖注入：ToolExecutor / ModelRouter / skill 解析器 / 安全器全部可注入，
  测试传 faker、生产传真实实例，无隐藏全局态。
"""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from workflow_v2.models import NodeSpec, NodeType, StepStatus
from workflow_v2.scheduler import NodeResult

_SECRET_RE = re.compile(r"\{\{secret:([\w.\-]+)\}\}")

# 高级 DAG 结构节点：本阶段不做执行，防静默失败
_NO_IMPL_HINT = "该节点类型在本阶段未实现，请在编排中改用 tool/mcp/model/skill/step"


class ExecutorError(Exception):
    """执行器内部错误，统一转为 NodeResult（不让 driver 崩）。"""


@dataclass
class ExecutorServices:
    """执行器依赖注入（全部可选；缺失的能力节点显式失败）。"""

    tool_executor: Any = None          # ToolExecutor.execute(tool_name, args, user_id=..)
    router: Any = None                 # ModelRouter.route / route_config
    security: Any = None               # SecurityFilter-like（check_user_input / check_output_privacy）
    secret_resolver: Callable[[str], str | None] | None = None
    skill_resolver: Callable[[str], dict[str, Any]] | None = None
    user_id: str = "workflow"


def _mask(text: Any, limit: int = 200) -> str:
    return str(text)[:limit]


def _retry_attempts(node: NodeSpec) -> int:
    rp = getattr(node, "retry_policy", None)
    if rp is None:
        return 1
    return max(1, getattr(rp, "max_attempts", 1))


class UnifiedExecutor:
    """Scheduler 的回调执行器（ExecutorSlot）：node → 能力层调用 → NodeResult。"""

    def __init__(self, services: ExecutorServices | None = None) -> None:
        self.svc = services or ExecutorServices()

    # ------------------------------------------------------------------ 入口
    async def __call__(self, node: NodeSpec, step: Any,
                       ctx: dict[str, Any]) -> NodeResult:
        try:
            return await self._dispatch(node, step, ctx)
        except ExecutorError as e:
            return NodeResult(status=StepStatus.FAILED, error_code="EXECUTOR_ERROR",
                              error_message=str(e)[:500])
        except Exception as e:  # noqa: BLE001 —— 执行器绝不允许把异常抛给驱动
            logger.warning("workflow.executor_failed node={} error={}",
                           node.id, str(e)[:300])
            return NodeResult(status=StepStatus.FAILED, error_code="EXECUTOR_ERROR",
                              error_message=str(e)[:500])

    async def _dispatch(self, node: NodeSpec, step: Any,
                        ctx: dict[str, Any]) -> NodeResult:
        t = node.type
        if t in (NodeType.START, NodeType.END):
            return NodeResult(status=StepStatus.SUCCEEDED, output={"node": node.id})
        if t == NodeType.TRANSFORM:
            # v1 "step" 节点=操作说明：输出 note 供下游/审计引用，不调用能力层
            return NodeResult(status=StepStatus.SUCCEEDED,
                              output={"node": node.id,
                                      "note": (node.config or {}).get("note", "")})
        if t in (NodeType.TOOL, NodeType.MCP):
            return await self._run_tool(node, t)
        if t == NodeType.MODEL:
            return await self._run_model(node)
        if t == NodeType.SKILL:
            return await self._run_skill(node)
        if t == NodeType.LEGACY_PROMPT:
            return await self._run_legacy(node)
        if t == NodeType.AGENT:
            return NodeResult(status=StepStatus.FAILED, error_code="AGENT_NOT_IMPLEMENTED",
                              error_message="子智能体节点尚未实现（M1 后置），请改用 step 串联")
        return NodeResult(status=StepStatus.FAILED, error_code="UNSUPPORTED_NODE",
                          error_message=f"node type '{t.value}' {_NO_IMPL_HINT}")

    # ------------------------------------------------------------------ 工具
    async def _run_tool(self, node: NodeSpec, t: NodeType) -> NodeResult:
        cfg = node.config or {}
        tool_name = cfg.get("tool_ref") or ""
        if not tool_name:
            return NodeResult(status=StepStatus.FAILED, error_code="TOOL_REF_MISSING",
                              error_message=f"{t.value} 节点缺少 tool_ref")
        if self.svc.tool_executor is None:
            return NodeResult(status=StepStatus.FAILED, error_code="TOOL_EXECUTOR_UNAVAILABLE",
                              error_message="工具执行器不可用（降级模式）")
        args = self._sanitize(await self._resolve_secrets(cfg.get("arguments") or {}), node)
        result = await self._with_timeout(
            node,
            lambda: self.svc.tool_executor.execute(tool_name, args, user_id=self.svc.user_id))
        if result is None:
            return NodeResult(status=StepStatus.FAILED, error_code="TIMEOUT",
                              error_message=f"工具 {tool_name} 执行超时(>{node.timeout_seconds}s)")
        ok = getattr(result, "success", False)
        if ok:
            return NodeResult(status=StepStatus.SUCCEEDED,
                              output={"tool": tool_name, "result": _mask(getattr(result, "data", ""))})
        return NodeResult(
            status=StepStatus.FAILED,
            error_code="APPROVAL_REJECTED" if getattr(result, "user_decision", False) else "TOOL_FAILED",
            error_message=_mask(getattr(result, "error", "tool failed")),
        )

    # ------------------------------------------------------------------ 模型
    def _llm_prompt(self, node: NodeSpec, extra: str = "") -> list[dict]:
        cfg = node.config or {}
        note = cfg.get("note") or node.name or "请直接给出处理结果"
        content = note
        if cfg.get("input"):
            content += f"\n\n{_mask(cfg['input'], 4000)}"
        msgs: list[dict] = []
        if extra:
            msgs.append({"role": "system", "content": _mask(extra, 8000)})
        msgs.append({"role": "user", "content": content})
        return msgs

    async def _run_model(self, node: NodeSpec) -> NodeResult:
        if self.svc.router is None:
            return NodeResult(status=StepStatus.FAILED, error_code="ROUTER_UNAVAILABLE",
                              error_message="LLM 路由不可用（降级模式）")
        cfg = node.config or {}
        policy = cfg.get("model_policy")
        if not isinstance(policy, dict):
            policy = {}
        msgs = self._llm_prompt(node)
        answer = await self._with_timeout(
            node, lambda: self.svc.router.route_config(
                policy, msgs, timeout=node.timeout_seconds))
        if answer is None:
            return NodeResult(status=StepStatus.FAILED, error_code="MODEL_TIMEOUT",
                              error_message="模型节点无响应(超时)")
        text, filtered = self._filter_output(str(answer))
        return NodeResult(status=StepStatus.SUCCEEDED,
                          output={"text": _mask(text), "privacy_filtered": filtered})

    # ------------------------------------------------------------------ 技能
    async def _run_skill(self, node: NodeSpec) -> NodeResult:
        cfg = node.config or {}
        skill_name = cfg.get("skill_ref") or node.name or ""
        if not skill_name:
            return NodeResult(status=StepStatus.FAILED, error_code="SKILL_REF_MISSING",
                              error_message="skill 节点缺少名称/ref")
        loader = self.svc.skill_resolver
        if loader is None:
            return NodeResult(status=StepStatus.FAILED, error_code="SKILL_RESOLVER_MISSING",
                              error_message="技能解析器不可用")
        if inspect.iscoroutinefunction(loader):
            skill = await loader(skill_name)
        else:
            skill = loader(skill_name)
        if not skill:
            return NodeResult(status=StepStatus.FAILED, error_code="SKILL_NOT_FOUND",
                              error_message=f"找不到技能 {skill_name}")
        instructions = skill.get("instructions") or skill.get("content") or ""
        if self.svc.router is None:
            return NodeResult(status=StepStatus.FAILED, error_code="ROUTER_UNAVAILABLE",
                              error_message="LLM 路由不可用（降级模式）")
        msgs = self._llm_prompt(node, extra=instructions)
        try:
            answer = await self._with_timeout(
                node, lambda: self.svc.router.route_config(
                    {}, msgs, timeout=node.timeout_seconds))
        except Exception as e:  # noqa: BLE001 —— 与主对话一致，失败落死因
            logger.warning("workflow.skill_llm_failed skill={} error={}",
                           skill_name, str(e)[:300])
            return NodeResult(status=StepStatus.FAILED, error_code="SKILL_LLM_FAILED",
                              error_message=str(e)[:500])
        if answer is None:
            return NodeResult(status=StepStatus.FAILED, error_code="SKILL_NO_ANSWER",
                              error_message="技能执行无输出")
        text, filtered = self._filter_output(str(answer))
        return NodeResult(status=StepStatus.SUCCEEDED,
                          output={"skill": skill_name, "text": _mask(text),
                                  "privacy_filtered": filtered})

    # ------------------------------------------------------------------ legacy
    async def _run_legacy(self, node: NodeSpec) -> NodeResult:
        raw = (node.config or {}).get("raw") or {}
        note = raw.get("note") or node.name or ""
        if self.svc.router is None:
            return NodeResult(status=StepStatus.FAILED, error_code="ROUTER_UNAVAILABLE",
                              error_message="LLM 路由不可用（降级模式）")
        prompt = (f"执行工作流节点「{node.name or node.id}」：{note}"
                  if note else f"请执行工作流节点「{node.name or node.id}」")
        try:
            answer = await self._with_timeout(
                node, lambda: self.svc.router.route(
                    task_type="chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3, timeout=node.timeout_seconds))
        except Exception as e:  # noqa: BLE001
            logger.warning("workflow.legacy_prompt_llm_failed node={} err={}",
                           node.id, str(e)[:300])
            return NodeResult(status=StepStatus.FAILED, error_code="LLM_CALL_FAILED",
                              error_message=str(e)[:500])
        text = str(answer or "").strip()
        if not text:
            return NodeResult(status=StepStatus.FAILED, error_code="LLM_EMPTY_OUTPUT",
                              error_message="模型无输出")
        text, filtered = self._filter_output(text)
        return NodeResult(status=StepStatus.SUCCEEDED,
                          output={"text": text, "prompt": prompt, "privacy_filtered": filtered})

    # ------------------------------------------------------------------ 横切
    async def _resolve_secrets(self, args: dict) -> dict:
        """把 {{secret:name}} 占位符替换为真实值；不可解析 → 拒绝执行（不落日志）。"""
        resolver = self.svc.secret_resolver
        if resolver is None:
            return args

        def _sub(m: re.Match) -> str:
            val = resolver(m.group(1))
            if val is None:
                raise ExecutorError(f"secret '{m.group(1)}' 未配置")
            return val

        out: dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str) and _SECRET_RE.search(v):
                out[k] = _SECRET_RE.sub(_sub, v)
            else:
                out[k] = v
        return out

    def _sanitize(self, args: dict, node: NodeSpec) -> dict:
        """文本参数先过 SecurityFilter.check_user_input；action=block 才拒绝执行。

        与主对话语义一致：warn 只提示不阻塞；无 filter/接口不兼容时弱失败放行。
        """
        if self.svc.security is None:
            return args
        out = dict(args)
        for k, v in list(out.items()):
            if isinstance(v, str) and v:
                try:
                    res = self.svc.security.check_user_input(v)
                except (TypeError, AttributeError):
                    continue  # 安全接口版本不符，放弃该项校验（不阻塞执行）
                if getattr(res, "action", "allow") == "block":
                    raise ExecutorError(
                        f"参数 {k} 触发安全拦截：{getattr(res, 'threat_type', 'blocked')}")
        return out

    def _filter_output(self, text: str) -> tuple[str, bool]:
        """LLM 输出过 check_output_privacy；高置信泄露 → 用安全替代回复，并标记。

        Returns:
            (处理后的文本, 是否触发过隐私过滤)
        """
        f = getattr(self.svc.security, "check_output_privacy", None)
        if f is None or not text:
            return text, False
        try:
            safe, replacement, _matched = f(text)
        except (TypeError, AttributeError):
            return text, False
        if not safe and replacement:
            return replacement, True
        return text, False

    async def _with_timeout(self, node: NodeSpec,
                            coro: Callable[[], Awaitable[Any]]) -> Any | None:
        """asyncio.wait_for + retry_policy 指数退避（仅超时重试）。超时耗尽返回 None。"""
        max_attempts = _retry_attempts(node)
        for attempt in range(1, max_attempts + 1):
            try:
                c = coro()
                return await asyncio.wait_for(c, timeout=node.timeout_seconds)
            except asyncio.TimeoutError:
                if attempt >= max_attempts:
                    return None
                await asyncio.sleep(min(2 ** (attempt - 1), 8))