import math

import pytest

from db.database import DatabaseManager


@pytest.mark.asyncio
async def test_profile_field_update_keeps_current_value_and_bitemporal_history(tmp_path):
    manager = DatabaseManager(tmp_path / "profile.db")
    await manager.init()

    first = await manager.profiles.put(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
        value=50000,
        value_type="integer",
        effective_at=10.0,
        known_at=12.0,
        source_type="conversation_log",
        source_id="message-1",
    )
    second = await manager.profiles.put(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
        value=80000,
        value_type="integer",
        effective_at=30.0,
        known_at=35.0,
        source_type="conversation_log",
        source_id="message-2",
    )

    current = await manager.profiles.get_current(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )
    before_update = await manager.profiles.get_as_of(
        20.0,
        known_at=34.0,
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )
    after_update = await manager.profiles.get_as_of(
        40.0,
        known_at=36.0,
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )
    current_knowledge_of_old_period = await manager.profiles.get_as_of(
        20.0,
        known_at=36.0,
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    )

    assert first.value == 50000
    assert second.value == 80000
    assert current == second
    assert before_update is not None
    assert before_update.id == first.id
    assert before_update.value == 50000
    assert after_update is not None
    assert after_update.id == second.id
    assert after_update.value == 80000
    assert current_knowledge_of_old_period is not None
    assert current_knowledge_of_old_period.value == first.value
    assert current_knowledge_of_old_period.valid_from == 10.0
    assert current_knowledge_of_old_period.valid_to == 30.0
    with pytest.raises(ValueError, match="earlier than current valid_from"):
        await manager.profiles.put(
            user_id="alice",
            agent_id="xiaoda",
            namespace="finance",
            field_key="purchase_budget",
            value=40000,
            value_type="integer",
            effective_at=5.0,
            known_at=40.0,
            source_type="conversation_log",
            source_id="message-retroactive",
        )
    await manager.close()


@pytest.mark.asyncio
async def test_profile_fields_are_isolated_by_user_agent_and_namespace(tmp_path):
    manager = DatabaseManager(tmp_path / "profile-isolation.db")
    await manager.init()

    await manager.profiles.put(
        user_id="alice",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
        value=80000,
        value_type="integer",
        known_at=10.0,
        source_type="conversation_log",
        source_id="alice-message",
    )

    assert await manager.profiles.get_current(
        user_id="bob",
        agent_id="xiaoda",
        namespace="finance",
        field_key="purchase_budget",
    ) is None
    assert await manager.profiles.get_current(
        user_id="alice",
        agent_id="xiaoli",
        namespace="finance",
        field_key="purchase_budget",
    ) is None
    assert await manager.profiles.get_current(
        user_id="alice",
        agent_id="xiaoda",
        namespace="shopping",
        field_key="purchase_budget",
    ) is None
    await manager.close()


@pytest.mark.asyncio
async def test_profile_store_rejects_non_finite_values_and_transaction_times(tmp_path):
    manager = DatabaseManager(tmp_path / "profile-validation.db")
    await manager.init()
    base = {
        "user_id": "alice",
        "agent_id": "xiaoda",
        "namespace": "finance",
        "field_key": "purchase_budget",
        "value_type": "number",
        "source_type": "conversation_log",
        "source_id": "invalid-message",
    }
    with pytest.raises(ValueError):
        await manager.profiles.put(**base, value=math.nan)
    with pytest.raises(ValueError):
        await manager.profiles.put(**base, value=1, known_at=math.inf)
    await manager.close()
