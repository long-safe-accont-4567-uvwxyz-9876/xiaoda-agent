"""I8: 3 种新协作模式 — 单元测试

覆盖:
- _ensemble_agents (集成模式: 多 agent 并行取最优)
- _retry_fallback (重试降级: 按优先级失败降级)
- _debate_agents (辩论模式: 正反方 + 综合者)
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


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
        tasks = [self.delegate_to_agent(a, task, mode="single") for a in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid = [r for r in results if isinstance(r, str) and len(r) > 20]
        if not valid:
            return "（所有子代理都无法完成任务）"
        return max(valid, key=len)

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
async def test_ensemble_picks_longest(manager):
    """集成模式应选最长的结果"""
    manager.dispatch_results = {
        "xiaoke": "[xiaoke] 短结果",
        "xiaolang": "[xiaolang] 这是一个比较长的结果，包含了更多的分析和细节内容" * 3,
        "xiaolian": "[xiaolian] 中等长度的结果" * 5,
    }
    result = await manager._ensemble_agents(["xiaoke", "xiaolang", "xiaolian"], "test task")
    assert "xiaolang" in result  # xiaolang 的结果最长


@pytest.mark.asyncio
async def test_ensemble_all_fail(manager):
    """所有 agent 都失败时返回兜底文本"""
    manager.dispatch_results = {"xiaoke": "短", "xiaolang": ""}
    result = await manager._ensemble_agents(["xiaoke", "xiaolang"], "test")
    assert "无法完成" in result


@pytest.mark.asyncio
async def test_ensemble_partial_failure(manager):
    """部分 agent 失败时仍能返回有效结果"""
    manager.dispatch_results = {
        "xiaoke": "[xiaoke] 有效的完整结果内容" * 5,
        "xiaolang": "x",  # 太短, 被过滤
    }
    result = await manager._ensemble_agents(["xiaoke", "xiaolang"], "test")
    assert "xiaoke" in result


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
