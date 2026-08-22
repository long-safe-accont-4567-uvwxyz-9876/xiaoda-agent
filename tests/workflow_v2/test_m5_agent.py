# tests/workflow_v2/test_m5_agent.py
"""M1.5 AGENT 节点执行器验收：三分派（子智能体/技能回退/模型回退）+ 失败路径。"""
import asyncio

import pytest

from workflow_v2.executor import ExecutorServices, UnifiedExecutor
from workflow_v2.models import NodeSpec, NodeType, StepStatus


class FakeAgent:
    """Fake SubAgent：记录 chat 调用，按配置回答/延迟/异常返回。"""

    def __init__(self, answer="ok", delay=0.0, exc: Exception | None = None,
                 available=True):
        self.answer = answer
        self.delay = delay
        self.exc = exc
        self.available = available
        self.calls = []

    async def chat(self, task, context=""):
        self.calls.append({"task": task, "context": context})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.answer


class FakeRouter:
    """Fake ModelRouter.route_config：记录调用，返回预制答案。"""

    def __init__(self, answer="ok"):
        self.answer = answer
        self.route_config_calls = []

    async def route_config(self, config, messages, **kwargs):
        self.route_config_calls.append((config, messages, kwargs))
        return self.answer


class FakeSecurity:
    """Fake SecurityFilter：仅 check_output_privacy 可用（AGENT 输出过滤路径）。"""

    def __init__(self, privacy_hit=""):
        self._hit = privacy_hit

    def check_user_input(self, text):
        return None  # 不阻断（本用例只验输出过滤）

    def check_output_privacy(self, text):
        if self._hit and self._hit in text:
            return False, f"涉及隐私内容已替换（{self._hit}）", [self._hit]
        return True, "", []


# NodeSpec 自有字段（其余一律落到 config）
_FIELD_KEYS = {"timeout_seconds", "retry_policy", "failure_policy", "route_to",
               "idempotency", "name", "input_schema", "output_schema"}


def _node(nid: str, ntype: NodeType, **kwargs):
    """构造 NodeSpec：自有字段单列，其余（含 agent_ref/skill_refs/...）进 config。"""
    spec_kw = {k: v for k, v in kwargs.items() if k in _FIELD_KEYS}
    spec_kw.setdefault("name", nid)
    spec_kw.setdefault("timeout_seconds", 60)
    config = {k: v for k, v in kwargs.items() if k not in _FIELD_KEYS}
    return NodeSpec(id=nid, type=ntype, config=config, **spec_kw)


def _svc(agents=None, router=None, security=None, skill_map=None):
    return ExecutorServices(
        router=router,
        security=security,
        skill_resolver=_skill_lookup(skill_map) if skill_map else None,
        subagent_loader=(lambda name: agents.get(name)) if agents else None,
    )


def _skill_lookup(skills: dict):
    def resolve(name: str):
        content = skills.get(name)
        return {"name": name, "instructions": content} if content is not None else None
    return resolve


# ---------------------------------------------------------------- 子智能体主路径
@pytest.mark.asyncio
async def test_agent_delegates_to_subagent():
    fake = FakeAgent("报告已生成")
    ex = UnifiedExecutor(_svc({"xiaoli": fake}))
    r = await ex(_node("a", NodeType.AGENT, agent_ref="xiaoli", task="整理资料"),
                 None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["agent"] == "xiaoli"
    assert r.output["text"] == "报告已生成"
    assert fake.calls == [{"task": "整理资料", "context": ""}]


@pytest.mark.asyncio
async def test_agent_passes_context_and_defaults_task():
    fake = FakeAgent()
    ex = UnifiedExecutor(_svc({"xiaoli": fake}))
    node = _node("n", NodeType.AGENT, agent_ref="xiaoli",
                 context="背景：见上轮", note="分析这份报告")
    await ex(node, None, {})
    assert fake.calls == [{"task": "分析这份报告", "context": "背景：见上轮"}]


@pytest.mark.asyncio
async def test_agent_timeout_surfaces():
    fake = FakeAgent(delay=2.0)
    ex = UnifiedExecutor(_svc({"xiaoli": fake}))
    node = _node("n", NodeType.AGENT, agent_ref="xiaoli")
    node.timeout_seconds = 0.05  # 严格 int 字段：构造成员后赋值（同 M1 模式）
    r = await ex(node, None, {})
    assert r.status == StepStatus.FAILED
    assert r.error_code == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_agent_not_found():
    ex = UnifiedExecutor(_svc({"xiaoli": FakeAgent()}))
    r = await ex(_node("n", NodeType.AGENT, agent_ref="不存在"), None, {})
    assert r.status == StepStatus.FAILED
    assert r.error_code == "AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_agent_loader_unavailable():
    ex = UnifiedExecutor(ExecutorServices())
    r = await ex(_node("n", NodeType.AGENT, agent_ref="x"), None, {})
    assert r.status == StepStatus.FAILED
    assert r.error_code == "AGENT_LOADER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_agent_chat_exception():
    fake = FakeAgent(exc=RuntimeError("LLM 挂了"))
    ex = UnifiedExecutor(_svc({"xiaoli": fake}))
    r = await ex(_node("n", NodeType.AGENT, agent_ref="xiaoli"), None, {})
    assert r.status == StepStatus.FAILED
    assert r.error_code == "AGENT_CHAT_FAILED"


@pytest.mark.asyncio
async def test_agent_unavailable_agent():
    fake = FakeAgent(available=False)
    ex = UnifiedExecutor(_svc({"xiaoli": fake}))
    r = await ex(_node("n", NodeType.AGENT, agent_ref="xiaoli"), None, {})
    assert r.status == StepStatus.FAILED
    assert r.error_code == "AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_agent_output_filtered():
    fake = FakeAgent("卡号 6222 已处理")
    ex = UnifiedExecutor(_svc({"xiaoli": fake}, security=FakeSecurity("6222")))
    r = await ex(_node("n", NodeType.AGENT, agent_ref="xiaoli"), None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["privacy_filtered"] is True


@pytest.mark.asyncio
async def test_agent_async_loader():
    async def loader(name):
        return FakeAgent("async-ok")

    ex = UnifiedExecutor(ExecutorServices(subagent_loader=loader))
    r = await ex(_node("n", NodeType.AGENT, agent_ref="x"), None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["agent"] == "x"


# ---------------------------------------------------------------- 迁移回退
@pytest.mark.asyncio
async def test_skill_refs_falls_back_to_skill():
    """v1 skill 迁移产物（skill_refs 列表）→ SKILL 语义。"""
    router = FakeRouter("[技能执行结果]")
    ex = UnifiedExecutor(_svc({}, router=router, skill_map={"s1": "指令"}))
    r = await ex(_node("n", NodeType.AGENT, skill_refs=["s1"]), None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["skill"] == "s1"
    assert r.output["text"] == "[技能执行结果]"


@pytest.mark.asyncio
async def test_model_policy_falls_back_to_model():
    """v1 model（model_policy → AGENT）→ MODEL 语义。"""
    router = FakeRouter("模型回复")
    ex = UnifiedExecutor(_svc({}, router=router))
    r = await ex(_node("n", NodeType.AGENT, model_policy={"ref": "m1"}), None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["text"] == "模型回复"


@pytest.mark.asyncio
async def test_agent_ref_overrides_skill_fallback():
    """同时有 skill_refs 与 agent_ref：agent_ref 优先。"""
    fake = FakeAgent("我是子代理")
    ex = UnifiedExecutor(_svc({"xiaoli": fake}))
    r = await ex(_node("n", NodeType.AGENT, agent_ref="xiaoli",
                       skill_refs=["s1"]), None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["agent"] == "xiaoli"


@pytest.mark.asyncio
async def test_agent_ref_missing_uses_node_name():
    """无 agent_ref 且无回退键时，用 node.name 兜底找子智能体。"""
    fake = FakeAgent("按名字找到")
    ex = UnifiedExecutor(_svc({"findme": fake}))
    r = await ex(_node("findme", NodeType.AGENT), None, {})
    assert r.status == StepStatus.SUCCEEDED
    assert r.output["agent"] == "findme"
