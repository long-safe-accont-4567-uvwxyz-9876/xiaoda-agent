"""Dream 数据库整合的零数据损失回归测试。"""

import time

import pytest

from core.dream_consolidation import DreamConsolidator


class FakeMemoryDB:
    def __init__(self, memories):
        self.memories = memories
        self.archived_batches = []
        self.deleted_batches = []

    async def get_all_memories(self, limit):
        return self.memories[:limit]

    async def archive_memories_batch(self, memory_ids):
        self.archived_batches.append(memory_ids)

    async def delete_memories_batch(self, memory_ids):
        self.deleted_batches.append(memory_ids)


class RecordingScorer:
    def __init__(self):
        self.similarities = []

    def score(self, similarity, created_at, access_count=0):
        self.similarities.append(similarity)
        return 1.0

    def should_archive(self, score):
        return False


@pytest.mark.asyncio
async def test_consolidate_db_scores_each_memory_with_its_real_importance():
    db = FakeMemoryDB([
        {"id": 1, "importance": 0.9, "timestamp": time.time(), "access_count": 0},
        {"id": 2, "importance": 0.1, "timestamp": time.time(), "access_count": 0},
    ])
    consolidator = DreamConsolidator()
    scorer = RecordingScorer()
    consolidator._fluid_scorer = scorer

    archived = await consolidator.consolidate_db(db)

    assert archived == 0
    assert scorer.similarities == [0.9, 0.1]


@pytest.mark.asyncio
async def test_prefix_similar_memories_return_relationships_without_physical_deletion():
    common_prefix = "这是十五字符相同的记忆前缀XYZ"
    db = FakeMemoryDB([
        {
            "id": 1,
            "summary": common_prefix + "，第一条证据",
            "importance": 0.9,
            "timestamp": time.time(),
            "access_count": 0,
        },
        {
            "id": 2,
            "summary": common_prefix + "，第二条证据",
            "importance": 0.4,
            "timestamp": time.time(),
            "access_count": 0,
        },
    ])
    consolidator = DreamConsolidator()

    result = await consolidator.consolidate_from_db(db)

    assert db.deleted_batches == []
    assert result["merged"] == 1
    assert result["similar_relationships"] == [
        {"source_memory_id": "1", "target_memory_id": "2", "edge_type": "similar"}
    ]


@pytest.mark.asyncio
async def test_low_score_memories_keep_legacy_recoverable_archive_behavior():
    db = FakeMemoryDB([
        {
            "id": 7,
            "summary": "很久以前且不重要的记忆",
            "importance": 0.01,
            "timestamp": time.time() - 365 * 86400,
            "access_count": 0,
        }
    ])
    consolidator = DreamConsolidator()

    result = await consolidator.consolidate_from_db(db)

    assert db.archived_batches == [[7]]
    assert db.deleted_batches == []
    assert result["evicted"] == 1
    assert result["decayed"] == 1
