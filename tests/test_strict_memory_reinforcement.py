import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.spontaneous_recall import SpontaneousRecall
from memory.fsrs_model import FSRSModel
from memory.memory_manager import MemoryManager


@pytest.mark.asyncio
async def test_fluid_scoring_is_read_only_for_retrieved_candidates():
    memory_db = SimpleNamespace(
        batch_increment_access_count=AsyncMock(),
        commit=AsyncMock(),
    )
    manager = object.__new__(MemoryManager)
    manager.memory = memory_db
    manager._fsrs = FSRSModel()

    results = await manager._apply_fsrs_scoring([
        {
            "id": 7,
            "timestamp": time.time(),
            "access_count": 2,
            "score": 0.9,
            "importance": 0.8,
        }
    ])

    assert [item["id"] for item in results] == [7]
    memory_db.batch_increment_access_count.assert_not_awaited()
    memory_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_spontaneous_recall_does_not_reinforce_or_claim_growth():
    memory_api = SimpleNamespace(increment_access_count=AsyncMock())
    core = SimpleNamespace(memory=memory_api)
    recall = SpontaneousRecall(core)
    recall._fetch_random_memory = AsyncMock(return_value={
        "id": 11,
        "summary": "爸爸喜欢简洁回答",
        "timestamp": 1.0,
    })
    recall._generate_monologue = AsyncMock(return_value="我想起爸爸喜欢简洁回答。")

    await recall._recall_once()

    memory_api.increment_access_count.assert_not_awaited()
