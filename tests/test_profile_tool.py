import pytest

from db.database import DatabaseManager
from memory.scope import Scope, bind_scope, reset_scope
from tools import profile_tool


@pytest.mark.asyncio
async def test_profile_tools_use_bound_scope_and_never_accept_identity_arguments(tmp_path):
    manager = DatabaseManager(tmp_path / "profile-tool.db")
    await manager.init()
    profile_tool.bind(manager.profiles)
    token = bind_scope(Scope(user_id="alice", session_id="chat-1", agent_id="xiaoda", request_id="request-1"))
    try:
        updated = await profile_tool.profile_set(
            namespace="finance",
            field_key="purchase_budget",
            value=80000,
            confidence=1.0,
        )
        fetched = await profile_tool.profile_get(
            namespace="finance",
            field_key="purchase_budget",
        )
        history = await profile_tool.profile_history(
            namespace="finance",
            field_key="purchase_budget",
        )
        forgotten = await profile_tool.profile_forget(
            namespace="finance",
            field_key="purchase_budget",
        )
        after_forget = await profile_tool.profile_get(
            namespace="finance",
            field_key="purchase_budget",
        )
    finally:
        reset_scope(token)

    assert updated.success is True
    assert fetched.data["value"] == 80000
    assert history.data[0]["value"] == 80000
    assert forgotten.success is True
    assert after_forget.data is None
    before_forget_knowledge = await manager.profiles.get_as_of(
        forgotten.data["forgotten_at"] + 10,
        known_at=forgotten.data["forgotten_at"] - 0.001,
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )
    assert before_forget_knowledge is not None
    assert before_forget_knowledge.value == 80000
    assert await manager.profiles.get_current(
        user_id="default",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    ) is None
    with pytest.raises(TypeError):
        await profile_tool.profile_get(
            namespace="finance",
            field_key="purchase_budget",
            user_id="bob",
        )
    await manager.close()


@pytest.mark.asyncio
async def test_profile_set_is_idempotent_within_same_trusted_request(tmp_path):
    manager = DatabaseManager(tmp_path / "profile-idempotency.db")
    await manager.init()
    profile_tool.bind(manager.profiles)
    token = bind_scope(Scope(user_id="alice", agent_id="xiaoda", request_id="request-2"))
    try:
        first = await profile_tool.profile_set(
            namespace="identity", field_key="preferred_name", value="Alice", confidence=1.0
        )
        second = await profile_tool.profile_set(
            namespace="identity", field_key="preferred_name", value="Alice", confidence=1.0
        )
    finally:
        reset_scope(token)
    history = await manager.profiles.get_history(
        user_id="alice", agent_id="xiaoda", namespace="identity", field_key="preferred_name"
    )
    events = await manager.profiles.list_events(user_id="alice", agent_id="xiaoda")
    assert first.data == second.data
    assert len(history) == 1
    assert len(events) == 1
    await manager.close()


def test_profile_tools_fail_closed_without_request_scope():
    with pytest.raises(RuntimeError, match="request scope"):
        profile_tool.current_scope()
