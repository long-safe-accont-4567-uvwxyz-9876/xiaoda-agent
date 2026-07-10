"""TDD tests for 3 bugs in task orchestration / DAG system.

Bug 3: DAG deadlock — SKIPPED dependencies not treated as satisfied.
Bug 7: SynthesisNode — LLM call should fall back to raw results on failure.
Bug 8: ParallelAgentNode — asyncio.gather without timeout hangs forever.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.parallel_dag import NodeState, ToolDAG
from task_orchestrator import ParallelAgentNode, SynthesisNode, TaskState


# ---------------------------------------------------------------------------
# Bug 3: DAG deadlock — SKIPPED dependencies should not block downstream nodes
# ---------------------------------------------------------------------------

async def test_ready_nodes_treats_skipped_dependency_as_satisfied():
    """_ready_nodes must return a PENDING node whose only dependency is SKIPPED.

    Without the fix, SKIPPED != SUCCESS so the node never becomes ready and
    the DAG deadloops forever waiting for a dependency that will never succeed.
    """
    dag = ToolDAG()
    dag.add_node("upstream", lambda: "ok")
    dag.add_node("downstream", lambda: "ok", depends_on=["upstream"])

    # Simulate upstream being skipped (e.g. its own upstream failed and
    # _skip_downstream did not propagate to downstream for whatever reason).
    dag._nodes["upstream"].state = NodeState.SKIPPED
    dag._nodes["downstream"].state = NodeState.PENDING

    ready = dag._ready_nodes()
    ready_names = [n.name for n in ready]
    assert "downstream" in ready_names, (
        "PENDING node with a SKIPPED dependency should be ready to proceed"
    )


async def test_skipped_dependency_does_not_deadlock():
    """End-to-end: a DAG where a dependency is SKIPPED must complete, not hang.

    Wraps execute() in a 5s timeout — without the fix the PENDING node never
    becomes ready and execute() loops forever (TimeoutError → test fails).
    """
    dag = ToolDAG()
    dag.add_node("a", lambda: "a_result")
    dag.add_node("b", lambda: "b_result", depends_on=["a"])

    # Manually mark "a" as SKIPPED, leaving "b" PENDING — simulates the edge
    # case where _skip_downstream did not reach "b".
    dag._nodes["a"].state = NodeState.SKIPPED

    result = await asyncio.wait_for(dag.execute(), timeout=5.0)

    assert dag._nodes["b"].state == NodeState.SUCCESS
    assert result.success_count == 1


async def test_mixed_skipped_and_success_dependencies_ready():
    """A node with one SKIPPED and one SUCCESS dependency should be ready."""
    dag = ToolDAG()
    dag.add_node("a", lambda: "a")
    dag.add_node("b", lambda: "b")
    dag.add_node("c", lambda: "c", depends_on=["a", "b"])

    dag._nodes["a"].state = NodeState.SKIPPED
    dag._nodes["b"].state = NodeState.SUCCESS
    dag._nodes["c"].state = NodeState.PENDING

    ready = dag._ready_nodes()
    ready_names = [n.name for n in ready]
    assert "c" in ready_names


# ---------------------------------------------------------------------------
# Bug 7: SynthesisNode should fall back to raw results on LLM failure
# ---------------------------------------------------------------------------

async def test_synthesis_node_falls_back_on_llm_failure():
    """When the LLM call raises, SynthesisNode must return raw concatenated
    results instead of crashing the whole task graph.
    """
    # Mock client whose chat.completions.create always raises.
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("LLM service unavailable")
    )

    node = SynthesisNode(client, model="test-model", xiaoda_chat_callback=None)

    results = [
        {"agent": "agent_a", "display_name": "AgentA", "reply": "reply from A"},
        {"agent": "agent_b", "display_name": "AgentB", "reply": "reply from B"},
    ]
    state = TaskState(
        user_input="test",
        user_id="u1",
        intermediate_results=results,
    )

    out = await node.synthesize(state)

    # Must not crash; must return a final_output containing the raw replies.
    assert "final_output" in out
    combined = out["final_output"]
    assert "reply from A" in combined
    assert "reply from B" in combined


# ---------------------------------------------------------------------------
# Bug 8: ParallelAgentNode must time out instead of hanging forever
# ---------------------------------------------------------------------------

def _make_mock_dispatcher(hang_target: str):
    """Build a MagicMock dispatcher where hang_target sleeps a long time and
    all other targets return immediately."""
    dispatcher = MagicMock()
    agent_mock = MagicMock(available=True, config=MagicMock(display_name="Test"))
    dispatcher.get_agent = MagicMock(return_value=agent_mock)

    async def _dispatch(target, *args, **kwargs):
        if target == hang_target:
            await asyncio.sleep(30)  # simulate a hung sub-agent
            return "should never reach"
        return f"{target} reply"

    dispatcher.dispatch = _dispatch
    return dispatcher


async def test_parallel_agent_node_times_out_on_hung_subagent():
    """If one sub-agent hangs, ParallelAgentNode must time out and return
    partial results instead of waiting forever."""
    dispatcher = _make_mock_dispatcher(hang_target="slow_agent")
    route_client = MagicMock()

    node = ParallelAgentNode(
        dispatcher, route_client, parallel_timeout=0.5,
    )
    # Bypass LLM-based task decomposition — not relevant to the timeout logic.
    async def _fake_decompose(user_input, targets):
        return {t: user_input for t in targets}
    node._decompose_task_v2 = _fake_decompose

    state = TaskState(
        user_input="test",
        user_id="u1",
        route_targets=["fast_agent", "slow_agent"],
        _agent_configs={},
    )

    # Test-level safety net: the node should return well within 10s.
    out = await asyncio.wait_for(node.execute(state), timeout=10.0)

    # The fast agent's result must be present (partial result).
    intermediate = out.get("intermediate_results", [])
    replies = [r.get("reply", "") for r in intermediate]
    assert any("fast_agent" in r for r in replies), (
        "partial result from the fast agent should be returned on timeout"
    )


async def test_parallel_agent_node_no_timeout_when_all_fast():
    """Smoke test: with no hanging agents the node completes normally."""
    dispatcher = _make_mock_dispatcher(hang_target="__none__")
    route_client = MagicMock()

    node = ParallelAgentNode(
        dispatcher, route_client, parallel_timeout=5.0,
    )
    async def _fake_decompose(user_input, targets):
        return {t: user_input for t in targets}
    node._decompose_task_v2 = _fake_decompose

    state = TaskState(
        user_input="test",
        user_id="u1",
        route_targets=["agent_a", "agent_b"],
        _agent_configs={},
    )

    out = await asyncio.wait_for(node.execute(state), timeout=10.0)
    intermediate = out.get("intermediate_results", [])
    assert len(intermediate) == 2
