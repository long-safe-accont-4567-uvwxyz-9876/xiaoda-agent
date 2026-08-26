# tests/test_cognitive_memory.py
"""3层认知记忆管理器测试"""
import time

import numpy as np
import pytest

from memory.cognitive_memory import CognitiveMemory, MemoryEntry


@pytest.fixture
def cog_mem():
    return CognitiveMemory(dimensions=64, episodic_capacity=100, semantic_max_clusters=10)

async def test_remember_episodic(cog_mem):
    """测试存储到Episodic层"""
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    mid = await cog_mem.remember("test content", emb, emotion_label="happy")
    assert mid > 0

async def test_recall_episodic(cog_mem):
    """测试Episodic层检索"""
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    await cog_mem.remember("hello world", emb)
    results = await cog_mem.recall(emb, k=5)
    assert len(results) > 0
    assert results[0][0] > 0  # memory_id

def test_connection_strength(cog_mem):
    """测试连接强度计算"""
    now = time.time()
    a = MemoryEntry(id=1, embedding=np.random.randn(64).astype(np.float32),
                    timestamp=now, last_accessed=now)
    b = MemoryEntry(id=2, embedding=a.embedding.copy(),
                    timestamp=now, last_accessed=now)
    strength = cog_mem.connection_strength(a, b)
    # 相同embedding → sim=1.0, temporal=1.0
    # strength = 1.0*0.5 + 1.0*0.3 + 0 = 0.8
    assert strength > 0.7

async def test_consolidate(cog_mem):
    """测试认知整合: episodic → semantic + hopfield"""
    # 存储多条记忆, 设置高access_count
    for i in range(5):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        mid = await cog_mem.remember(f"content_{i}", emb)
        # 模拟多次访问
        cog_mem._touch(mid, count=5)

    transferred = await cog_mem.consolidate(batch_size=10)
    assert transferred > 0

def test_self_attention_sweep(cog_mem):
    """测试自注意力扫描"""
    now = time.time()
    memories = []
    for i in range(5):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        memories.append(MemoryEntry(id=i+1, embedding=emb, timestamp=now, last_accessed=now))
    # 添加一条与第一条相同的
    memories.append(MemoryEntry(id=6, embedding=memories[0].embedding.copy(),
                                timestamp=now, last_accessed=now))
    connections = cog_mem.self_attention_sweep(memories, threshold=0.5)
    # id=1 和 id=6 应有高连接强度
    assert len(connections) > 0
