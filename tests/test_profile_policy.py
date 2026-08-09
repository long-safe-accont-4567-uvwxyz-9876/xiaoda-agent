import pytest
import math

from db.database import DatabaseManager
from memory.profile_policy import ProfileCandidate, ProfilePolicy
from memory.scope import Scope


@pytest.mark.asyncio
async def test_policy_accepts_valid_budget_and_rejects_low_confidence_candidate(tmp_path):
    manager = DatabaseManager(tmp_path / "policy.db")
    await manager.init()
    policy = ProfilePolicy(manager.profiles)
    scope = Scope(user_id="alice", agent_id="xiaoda", session_id="chat-1")

    accepted = await policy.apply(
        scope,
        ProfileCandidate(
            namespace="finance",
            field_key="purchase_budget",
            value=80000,
            confidence=0.95,
            source_type="conversation_log",
            source_id="message-1",
            effective_at=10.0,
        ),
        known_at=12.0,
    )
    rejected = await policy.apply(
        scope,
        ProfileCandidate(
            namespace="finance",
            field_key="purchase_budget",
            value=90000,
            confidence=0.4,
            source_type="conversation_log",
            source_id="message-2",
        ),
        known_at=13.0,
    )

    assert accepted.status == "accepted"
    assert accepted.field is not None
    assert accepted.field.value == 80000
    assert rejected.status == "rejected"
    assert rejected.reason == "confidence_below_threshold"
    current = await manager.profiles.get_current(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )
    assert current is not None
    assert current.value == 80000
    events = await manager.profiles.list_events(user_id="alice", agent_id="xiaoda")
    assert [event["status"] for event in events] == ["accepted", "rejected"]
    await manager.close()


@pytest.mark.asyncio
async def test_policy_rejects_non_finite_confidence(tmp_path):
    manager = DatabaseManager(tmp_path / "policy-confidence.db")
    await manager.init()
    policy = ProfilePolicy(manager.profiles)
    decision = await policy.apply(
        Scope(user_id="alice"),
        ProfileCandidate(
            namespace="finance",
            field_key="purchase_budget",
            value=80000,
            confidence=math.nan,
            source_type="conversation_log",
            source_id="invalid-confidence",
        ),
    )
    assert decision.status == "rejected"
    assert decision.reason == "invalid_confidence"
    await manager.close()


@pytest.mark.asyncio
async def test_policy_rejects_oversized_profile_values(tmp_path):
    manager = DatabaseManager(tmp_path / "policy-size.db")
    await manager.init()
    decision = await ProfilePolicy(manager.profiles).apply(
        Scope(user_id="alice"),
        ProfileCandidate(
            namespace="preferences",
            field_key="communication_style",
            value="x" * 5000,
            confidence=1.0,
            source_type="conversation_log",
            source_id="oversized",
        ),
    )
    assert decision.status == "rejected"
    assert decision.reason == "value_too_large"
    await manager.close()


@pytest.mark.asyncio
async def test_policy_requires_registered_typed_custom_fields(tmp_path):
    manager = DatabaseManager(tmp_path / "custom-policy.db")
    await manager.init()
    policy = ProfilePolicy(manager.profiles)
    scope = Scope(user_id="alice")
    candidate = ProfileCandidate(
        namespace="travel",
        field_key="seat_preference",
        value="window",
        confidence=0.9,
        source_type="conversation_log",
        source_id="message-3",
    )

    unknown = await policy.apply(scope, candidate)
    policy.register_field("travel", "seat_preference", value_type="string")
    accepted = await policy.apply(scope, candidate)

    assert unknown.status == "rejected"
    assert unknown.reason == "unknown_field"
    assert accepted.status == "accepted"
    with pytest.raises(ValueError):
        policy.register_field("travel", "bad", value_type="executable")
    await manager.close()


@pytest.mark.asyncio
async def test_policy_rolls_back_field_when_audit_event_fails(tmp_path):
    manager = DatabaseManager(tmp_path / "policy-atomic.db")
    await manager.init()
    await manager.execute(
        """CREATE TRIGGER reject_profile_event BEFORE INSERT ON profile_events
           BEGIN SELECT RAISE(ABORT, 'audit failed'); END"""
    )
    policy = ProfilePolicy(manager.profiles)
    with pytest.raises(Exception, match="audit failed"):
        await policy.apply(
            Scope(user_id="alice"),
            ProfileCandidate(
                namespace="finance",
                field_key="purchase_budget",
                value=80000,
                confidence=1.0,
                source_type="conversation_log",
                source_id="message-atomic",
            ),
        )
    assert await manager.profiles.get_current(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    ) is None
    await manager.close()


@pytest.mark.asyncio
async def test_forget_rolls_back_when_audit_event_fails(tmp_path):
    manager = DatabaseManager(tmp_path / "forget-atomic.db")
    await manager.init()
    field = await manager.profiles.put(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
        value=80000,
        value_type="integer",
        source_type="conversation_log",
        source_id="message-before-forget",
    )
    await manager.execute(
        """CREATE TRIGGER reject_forget_event BEFORE INSERT ON profile_events
           WHEN NEW.status = 'forgotten'
           BEGIN SELECT RAISE(ABORT, 'forget audit failed'); END"""
    )
    with pytest.raises(Exception, match="forget audit failed"):
        await manager.profiles.forget_with_event(
            user_id="alice",
            agent_id="xiaoda",
            session_id="chat-1",
            namespace="finance",
            field_key="purchase_budget",
            source_type="agent_tool",
            source_id="explicit-forget",
        )
    current = await manager.profiles.get_current(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )
    assert current is not None
    assert current.id == field.id
    await manager.close()
