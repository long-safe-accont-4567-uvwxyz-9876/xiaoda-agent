"""QQ 群回复记忆检索隐私 gating（P1 存量隐私缺口回归）。

主人在 QQ 群里提问时，_retrieve_main_memories 默认必须：
1. 把检索 scope 降为该群 conversation 形态（Scope.group）；
2. 对检索结果按 matches_record 后过滤，剔除 personal-boundary（私聊提炼）记忆；
3. GROUP_REPLY_PERSONAL_MEMORY_ENABLED=true 恢复旧行为；
4. C2C 私聊路径完全不受影响。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from agent_context import AgentContext
from agent_core.mixins.main_path import MainPathMixin
from memory.scope import Scope, ScopeBoundary, bind_scope, reset_scope


def _mixed_results() -> list[dict]:
    return [
        {
            "id": 1,
            "summary": "私聊里的私密记忆",
            "user_id": "owner-principal",
            "agent_id": "xiaoda",
            "session_id": "c2c-sess-7",
        },
        {
            "id": 2,
            "summary": "本群对话提炼的记忆",
            "user_id": "owner-principal",
            "agent_id": "xiaoda",
            "session_id": "qq_group:g1",
        },
    ]


class _RetrievalHarness(MainPathMixin):
    """最小化 MainPathMixin 桩：只撑起 _retrieve_main_memories 的依赖面。"""

    def __init__(self, results: list[dict]) -> None:
        self.context = AgentContext(system_prompt="test")
        self.memory = MagicMock()
        self.memory.signal_new_message = MagicMock()
        self.memory._suggest_k = lambda *_args, **_kwargs: 3
        self.memory.audit_retrieval = AsyncMock()
        captured: dict = {}

        async def _fake_retrieve(query: str, **kwargs: object) -> list[dict]:
            captured["query"] = query
            captured.update(kwargs)
            return [dict(row) for row in results]

        self.memory.retrieve_memories = _fake_retrieve
        self._load_notebook_context = AsyncMock()
        self.captured = captured


async def test_shadow_bundle_captures_degraded_trace_from_retrieval_task() -> None:
    harness = _RetrievalHarness([_mixed_results()[1]])
    original_retrieve = harness.memory.retrieve_memories

    async def degraded_retrieve(*args, **kwargs):
        rows = await original_retrieve(*args, **kwargs)
        for row in rows:
            row["degraded_components"] = ["reranker"]
        return rows

    harness.memory.retrieve_memories = degraded_retrieve
    context_token = await harness.context.switch_user_context("owner-principal")
    scope_token = bind_scope(Scope.personal(user_id="owner-principal"))
    try:
        await harness._retrieve_main_memories(
            "最近怎么样", True, {}, user_token=context_token
        )
    finally:
        reset_scope(scope_token)

    assert harness.context.evidence_bundle.degraded_components == ("reranker",)


async def test_group_retrieval_builds_shadow_evidence_without_changing_results() -> None:
    harness = _RetrievalHarness(_mixed_results())
    context_token = await harness.context.switch_user_context("owner-principal")
    bound = Scope(
        user_id="owner-principal", session_id="qq_group:g1", agent_id="xiaoda"
    )
    scope_token = bind_scope(bound)
    try:
        memories = await harness._retrieve_main_memories(
            "最近怎么样", True, {}, user_token=context_token
        )
    finally:
        reset_scope(scope_token)

    assert [memory["id"] for memory in memories] == [2]
    bundle = harness.context.evidence_bundle
    assert bundle is not None
    assert [item.source_id for item in bundle.evidence] == ["2"]
    assert bundle.plan.scope.boundary == ScopeBoundary.CONVERSATION.value


async def test_master_group_message_downgrades_scope_and_drops_personal_memories() -> None:
    harness = _RetrievalHarness(_mixed_results())
    bound = Scope(
        user_id="owner-principal",
        session_id="qq_group:g1",
        agent_id="xiaoda",
    )
    token = bind_scope(bound)
    try:
        memories = await harness._retrieve_main_memories("最近怎么样", True, {})
    finally:
        reset_scope(token)

    scope = harness.captured["scope"]
    assert scope.boundary is ScopeBoundary.CONVERSATION
    assert scope.session_id == "qq_group:g1"
    assert harness.captured["conv_user_id"] == "owner-principal"
    assert [m["id"] for m in memories] == [2]
    assert all("私密" not in m["summary"] for m in memories)


async def test_group_privacy_cannot_be_disabled_by_legacy_switch(monkeypatch) -> None:
    monkeypatch.setattr(config, "GROUP_REPLY_PERSONAL_MEMORY_ENABLED", True)
    harness = _RetrievalHarness(_mixed_results())
    bound = Scope(
        user_id="owner-principal",
        session_id="qq_group:g1",
        agent_id="xiaoda",
    )
    token = bind_scope(bound)
    try:
        memories = await harness._retrieve_main_memories("最近怎么样", True, {})
    finally:
        reset_scope(token)

    scope = harness.captured["scope"]
    assert scope.boundary is ScopeBoundary.CONVERSATION
    assert [m["id"] for m in memories] == [2]


async def test_c2c_private_path_is_unaffected() -> None:
    harness = _RetrievalHarness(_mixed_results())
    bound = Scope.personal(user_id="owner-principal", session_id="c2c-sess-7")
    token = bind_scope(bound)
    try:
        memories = await harness._retrieve_main_memories("最近怎么样", True, {})
    finally:
        reset_scope(token)

    assert harness.captured["scope"] is bound
    assert harness.captured["scope"].boundary is ScopeBoundary.PERSONAL
    assert [m["id"] for m in memories] == [1, 2]


@pytest.mark.parametrize("enabled", [False, True])
async def test_convlog_style_records_without_session_are_always_dropped(
    monkeypatch, enabled: bool,
) -> None:
    monkeypatch.setattr(config, "GROUP_REPLY_PERSONAL_MEMORY_ENABLED", enabled)
    results = [
        {
            "id": 9,
            "summary": "时间线通道的原始私聊片段",
            "type": "conversation_log",
        },
        {
            "id": 2,
            "summary": "本群对话提炼的记忆",
            "user_id": "owner-principal",
            "agent_id": "xiaoda",
            "session_id": "qq_group:g1",
        },
    ]
    harness = _RetrievalHarness(results)
    token = bind_scope(Scope(user_id="owner-principal", session_id="qq_group:g1"))
    try:
        memories = await harness._retrieve_main_memories("最近怎么样", True, {})
    finally:
        reset_scope(token)

    assert [m["id"] for m in memories] == [2]
