# tests/test_dream_engine_v2.py
"""6阶段梦境引擎测试"""
import asyncio
import time
import numpy as np
import pytest
from core.dream_engine_v2 import DreamEngineV2
from memory.cognitive_memory import CognitiveMemory, MemoryEntry

@pytest.fixture
def dream():
    cog = CognitiveMemory(dimensions=64, episodic_capacity=100, semantic_max_clusters=10)
    return DreamEngineV2(cognitive_memory=cog)

def test_sample_three_slice(dream):
    """测试三切片采样"""
    memories = []
    now = time.time()
    for i in range(20):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        m = MemoryEntry(id=i+1, embedding=emb, content=f"mem_{i}",
                        timestamp=now-i*100, last_accessed=now-i*100,
                        salience=np.random.random())
        memories.append(m)

    sampled = dream._sample_for_dream(memories, limit=10)
    assert len(sampled) <= 10
    assert len(sampled) > 0

async def test_run_cycle_empty(dream):
    """测试空记忆的梦境周期"""
    stats = await dream.run_cycle()
    assert "duration_ms" in stats
    assert stats["nrem_sampled"] == 0

async def test_run_cycle_with_memories(dream):
    """测试有记忆的梦境周期"""
    now = time.time()
    for i in range(10):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        await dream._cognitive.remember(f"content_{i}", emb, emotion_label="happy")
    stats = await dream.run_cycle()
    assert stats["nrem_sampled"] > 0
