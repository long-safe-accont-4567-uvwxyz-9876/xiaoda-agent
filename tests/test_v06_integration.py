# tests/test_v06_integration.py
"""v0.6.0 认知架构集成测试"""
import asyncio
import time
import numpy as np
import pytest
from memory.cognitive_memory import CognitiveMemory, MemoryEntry
from memory.hopfield_layer import HopfieldLayer
from memory.salience import SalienceScorer
from memory.bridge_memory import BridgeMemoryManager
from memory.spreading_activation import SpreadingActivation
from core.conflict_supersession import ConflictSupersession
from core.dream_engine_v2 import DreamEngineV2
from memory.preference_discovery import PreferenceDiscovery

@pytest.fixture
def system():
    """构建完整认知系统"""
    cog = CognitiveMemory(dimensions=64, episodic_capacity=100, semantic_max_clusters=10)
    dream = DreamEngineV2(cognitive_memory=cog)
    return {"cog": cog, "dream": dream}

async def test_full_cycle(system):
    """测试完整梦境周期"""
    cog = system["cog"]
    dream = system["dream"]

    # 存储记忆
    for i in range(20):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        await cog.remember(f"memory_{i}", emb, emotion_label="happy" if i % 2 == 0 else "sad")

    # 运行梦境周期
    stats = await dream.run_cycle()
    assert stats["cycle"] == 1
    assert stats["duration_ms"] > 0

async def test_consolidation_promotes_to_semantic(system):
    """测试整合提升记忆到Semantic层"""
    cog = system["cog"]

    # 存储并多次访问
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    mid = await cog.remember("important memory", emb, emotion_label="happy")
    cog._touch(mid, count=5)

    initial_semantic = cog.semantic_size()
    await cog.consolidate(batch_size=10)
    assert cog.semantic_size() > initial_semantic

async def test_hopfield_integration(system):
    """测试Hopfield联想与CognitiveMemory集成"""
    cog = system["cog"]

    # 存储一组模式
    patterns = []
    for i in range(5):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        patterns.append(emb)
        await cog.remember(f"pattern_{i}", emb)

    # 用其中一个pattern的噪声版本检索
    query = patterns[0] + np.random.randn(64).astype(np.float32) * 0.1
    query /= np.linalg.norm(query)
    results = await cog.recall(query, k=5)
    assert len(results) > 0

async def test_salience_with_emotion(system):
    """测试情绪加权Salience"""
    cog = system["cog"]
    scorer = SalienceScorer()

    now = time.time()
    entry_happy = MemoryEntry(
        id=1, embedding=np.random.randn(64).astype(np.float32),
        emotion_label="happy", timestamp=now, last_accessed=now,
        access_count=2
    )

    class MockPAD:
        arousal = 0.8
        dominant_emotion = "happy"

    score = scorer.compute(entry_happy, now, pad_state=MockPAD())
    assert score > 0.3  # 高arousal + 标签匹配 → 高分
