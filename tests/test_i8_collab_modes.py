"""I8: 3 种新协作模式 — 单元测试

覆盖:
- _ensemble_agents (集成模式: 多 agent 并行取最优)
- _retry_fallback (重试降级: 按优先级失败降级)
- _debate_agents (辩论模式: 正反方 + 综合者)
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_core._shared import (
    ALL_SUB_AGENTS_FAILED_MSG,
    TIRED_MSG,
    DelegationOutcome,
    is_degraded_reply,
)
from agent_core.sub_agent_manager import SubAgentManagerMixin
from agent_core.subagents import SubAgentInvocationResult


class FakeSubAgentManager:
    """轻量测试替身, 只实现 delegate_to_agent 和 3 个新模式"""

    def __init__(self):
        self.dispatch_results: dict[str, str] = {}

    async def delegate_to_agent(self, name: str, task: str,
                                 mode: str = "single", verifier: str = "") -> str:
        """模拟委托 — 返回预设结果, 或基于 task 内容生成"""
        # 如果 task 是辩论/综合的 prompt, 返回基于 name 的结果
        if "正方" in task or "正面" in task:
            return f"[{name}的正方观点] 支持这个方案"
        if "反方" in task or "反面" in task:
            return f"[{name}的反方观点] 质疑这个方案"
        if "正反两方" in task or "综合" in task:
            return f"[{name}的综合结论] 平衡正反方观点"
        # 普通委托: 返回预设或默认
        return self.dispatch_results.get(name, f"[{name}的回复] " + "x" * 30)

    async def _ensemble_agents(self, agents, task):
        return await SubAgentManagerMixin._ensemble_agents(self, agents, task)

    async def delegate_to_agent_outcome(self, name, task, mode="single", verifier=""):
        reply = await self.delegate_to_agent(name, task, mode=mode, verifier=verifier)
        return DelegationOutcome(ok=bool(reply and reply.strip()), reply=reply, agent=name,
                                 degraded=is_degraded_reply(reply))

    async def _retry_fallback(self, agents, task):
        for agent_name in agents:
            try:
                result = await self.delegate_to_agent(agent_name, task, mode="single")
                if result and len(result) > 20:
                    return result
            except Exception:
                pass
        return "（所有子代理都未能完成任务）"

    async def _debate_agents(self, agents, synthesizer, task):
        if len(agents) < 2:
            return await self.delegate_to_agent(
                agents[0] if agents else "xiaoda", task, mode="single")
        pro_prompt = f"请从正面/支持角度分析以下问题，给出你的论点和论据：\n{task}"
        con_prompt = f"请从反面/质疑角度分析以下问题，给出你的论点和论据：\n{task}"
        pro_task = self.delegate_to_agent(agents[0], pro_prompt, mode="single")
        con_task = self.delegate_to_agent(agents[1], con_prompt, mode="single")
        pro_result, con_result = await asyncio.gather(
            pro_task, con_task, return_exceptions=True)
        if not isinstance(pro_result, str) or len(pro_result) < 10:
            pro_result = "（正方无法给出观点）"
        if not isinstance(con_result, str) or len(con_result) < 10:
            con_result = "（反方无法给出观点）"
        synth_name = synthesizer or "xiaoda"
        synth_prompt = (
            f"以下是关于「{task}」的正反两方观点，请综合分析并给出平衡的结论：\n\n"
            f"【正方观点】\n{pro_result}\n\n"
            f"【反方观点】\n{con_result}\n\n"
            f"请综合以上观点，给出你的判断和建议。"
        )
        return await self.delegate_to_agent(synth_name, synth_prompt, mode="single")


@pytest.fixture
def manager():
    return FakeSubAgentManager()


# ============================================================
# ensemble
# ============================================================

@pytest.mark.asyncio
async def test_ensemble_all_fail_returns_unified_message():
    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)

    async def _fail(name, task, mode="single", verifier=""):
        return DelegationOutcome(ok=False, reply="XiaoLang" + TIRED_MSG, agent=name, degraded=True)

    mgr.delegate_to_agent_outcome = _fail
    result = await mgr._ensemble_agents(["xiaoke", "xiaolang"], "test task")
    assert result == ALL_SUB_AGENTS_FAILED_MSG
    assert TIRED_MSG not in result


@pytest.mark.asyncio
async def test_ensemble_success_beats_longer_failure_text():
    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    ok_reply = "OK-" + ("a" * 40)
    fail_reply = ("XiaoLang" + TIRED_MSG) * 5
    assert len(fail_reply) > len(ok_reply)

    async def _mixed(name, task, mode="single", verifier=""):
        if name == "xiaoke":
            return DelegationOutcome(ok=True, reply=ok_reply, agent=name)
        return DelegationOutcome(ok=False, reply=fail_reply, agent=name, degraded=True)

    mgr.delegate_to_agent_outcome = _mixed
    assert await mgr._ensemble_agents(["xiaoke", "xiaolang"], "t") == ok_reply


@pytest.mark.asyncio
async def test_ensemble_filters_degraded_text_even_when_ok_flag_set():
    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)

    async def _stub(name, task, mode="single", verifier=""):
        if name == "xiaoke":
            text = "XiaoKe" + TIRED_MSG
            return DelegationOutcome(ok=True, reply=text, agent=name, degraded=is_degraded_reply(text))
        return DelegationOutcome(ok=True, reply="real answer body", agent=name)

    mgr.delegate_to_agent_outcome = _stub
    assert await mgr._ensemble_agents(["xiaoke", "xiaolang"], "t") == "real answer body"


def test_outcome_from_reply_marks_tired_as_degraded():
    outcome = SubAgentManagerMixin._outcome_from_reply("xiaolang", "XiaoLang" + TIRED_MSG)
    assert outcome.ok is True
    assert outcome.degraded is True
    assert outcome.usable() is False
    assert is_degraded_reply(TIRED_MSG) is True
    assert is_degraded_reply("XiaoLi" + TIRED_MSG) is True
    assert is_degraded_reply("Here is a normal analysis of transformer losses.") is False


@pytest.mark.asyncio
async def test_pipe_does_not_feed_failure_text_downstream():
    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    seen = {}

    async def _stub(name, task, mode="single", verifier=""):
        seen[name] = task
        if name == "xiaoke":
            return DelegationOutcome(ok=False, reply="XiaoKe" + TIRED_MSG, agent=name, degraded=True)
        return DelegationOutcome(ok=True, reply="final answer body", agent=name)

    mgr.delegate_to_agent_outcome = _stub
    result = await mgr._sequential_pipe(["xiaoke", "xiaolang"], "original task")
    assert result == "final answer body"
    assert TIRED_MSG not in seen["xiaolang"]
    assert "original task" in seen["xiaolang"]


@pytest.mark.asyncio
async def test_pipe_all_fail_returns_last_visible_reply():
    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    seen = []

    async def _fail(name, task, mode="single", verifier=""):
        seen.append(task)
        return DelegationOutcome(ok=False, reply="X" + TIRED_MSG, agent=name, degraded=True)

    mgr.delegate_to_agent_outcome = _fail
    result = await mgr._sequential_pipe(["xiaoke", "xiaolang"], "original task")
    assert result == "X" + TIRED_MSG
    assert len(seen) == 2
    assert TIRED_MSG not in seen[1]


# ============================================================
# retry_fallback
# ============================================================

@pytest.mark.asyncio
async def test_retry_fallback_first_success(manager):
    """第一个 agent 成功时直接返回"""
    manager.dispatch_results = {"xiaoke": "[xiaoke] 成功的结果" * 10}
    result = await manager._retry_fallback(["xiaoke", "xiaolang"], "test")
    assert "xiaoke" in result


@pytest.mark.asyncio
async def test_retry_fallback_falls_to_second(manager):
    """第一个失败时降级到第二个"""
    manager.dispatch_results = {"xiaoke": "短", "xiaolang": "[xiaolang] 降级后的完整结果" * 5}
    result = await manager._retry_fallback(["xiaoke", "xiaolang"], "test")
    assert "xiaolang" in result


@pytest.mark.asyncio
async def test_retry_fallback_all_fail(manager):
    """全部失败时返回兜底文本"""
    manager.dispatch_results = {"xiaoke": "", "xiaolang": "x"}
    result = await manager._retry_fallback(["xiaoke", "xiaolang"], "test")
    assert "未能完成" in result


@pytest.mark.asyncio
async def test_retry_fallback_uses_status_not_long_error_display_text():
    manager = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    manager.context = SimpleNamespace(current_address_term="爸爸")
    manager._build_sub_agent_context = MagicMock(return_value="structured context")
    manager.delegate_to_agent = AsyncMock(return_value="错误：" + "x" * 500)

    agent = SimpleNamespace(
        config=SimpleNamespace(
            allowed_paths=[], forbidden_paths=[], permission_mode="default",
        ),
        _filtered_tool_names=lambda: set(),
    )
    first_failure = SubAgentInvocationResult(
        target="xiaoke",
        status="failed",
        error_code="SUB_AGENT_EXECUTION_FAILED",
        error_message="错误：" + "x" * 500,
    )
    second_success = SubAgentInvocationResult.completed(
        target="xiaolang", final_report="第二代理完成",
    )
    manager.dispatcher = MagicMock()
    manager.dispatcher.get_agent.return_value = agent
    manager.dispatcher.dispatch_invocation = AsyncMock(
        side_effect=[first_failure, second_success],
    )

    result = await manager._retry_fallback(["xiaoke", "xiaolang"], "test")

    assert result == "第二代理完成"
    assert manager.dispatcher.dispatch_invocation.await_count == 2
    assert [
        call.args[0].target
        for call in manager.dispatcher.dispatch_invocation.await_args_list
    ] == ["xiaoke", "xiaolang"]


# ============================================================
# debate
# ============================================================

@pytest.mark.asyncio
async def test_debate_produces_synthesis(manager):
    """辩论模式应产生综合结论"""
    result = await manager._debate_agents(
        ["xiaoke", "xiaolang"], synthesizer="xiaoda", task="是否应该使用微服务架构")
    assert "综合" in result or "xiaoda" in result


@pytest.mark.asyncio
async def test_debate_single_agent_degrades(manager):
    """只有一个 agent 时退化为直接委托"""
    result = await manager._debate_agents(["xiaoke"], synthesizer="xiaoda", task="test")
    assert "xiaoke" in result


@pytest.mark.asyncio
async def test_debate_uses_synthesizer(manager):
    """辩论模式应使用指定的综合者"""
    result = await manager._debate_agents(
        ["xiaoke", "xiaolang"], synthesizer="xiaolian", task="test question")
    # 综合者 xiaolian 应在结果中
    assert "xiaolian" in result


@pytest.mark.asyncio
async def test_debate_default_synthesizer(manager):
    """未指定综合者时默认用 xiaoda"""
    result = await manager._debate_agents(
        ["xiaoke", "xiaolang"], synthesizer="", task="test question")
    assert "xiaoda" in result
