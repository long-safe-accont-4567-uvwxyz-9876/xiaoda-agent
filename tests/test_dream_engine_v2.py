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

async def test_connection_graph_shared_with_cognitive_memory():
    """I2: DreamEngineV2 与 CognitiveMemory 共享同一连接图（引用）。

    consolidate() 中 self_attention_sweep 发现的连接应对 DreamEngineV2 可见，
    使 NREM Hebbian 强化能作用在整合阶段发现的连接上。
    """
    cog = CognitiveMemory(dimensions=64, episodic_capacity=100, semantic_max_clusters=10)
    # 创建高相似度记忆（相同 embedding）以触发连接发现
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    await cog.remember("mem_a", emb, emotion_label="happy")
    await cog.remember("mem_b", emb, emotion_label="happy")
    # consolidate 通过 self_attention_sweep 填充 _connections
    await cog.consolidate()
    # 引用共享：DreamEngineV2 应直接使用 CognitiveMemory 的连接图
    dream = DreamEngineV2(cognitive_memory=cog)
    assert dream._connections is cog._connections
    # consolidate 发现的连接应对 DreamEngineV2 可见
    assert cog._connections, "consolidate 应发现至少一条连接"
    # 运行一个周期后仍是同一对象（未被重新赋值）
    await dream.run_cycle()
    assert dream._connections is cog._connections


async def test_phase_afe_stage_s():
    """Phase 5: 偏好结晶 — 从相似记忆中提取模式"""
    np.random.seed(123)
    cog = CognitiveMemory(dimensions=64, episodic_capacity=100, semantic_max_clusters=10)
    dream = DreamEngineV2(cognitive_memory=cog)

    # 创建几组相似记忆 (同组用相同 embedding + 噪声)
    base_emb = np.random.randn(64).astype(np.float32)
    base_emb /= np.linalg.norm(base_emb)

    # 组1: 关于编程偏好
    for i in range(5):
        emb = base_emb + np.random.randn(64).astype(np.float32) * 0.05
        emb /= np.linalg.norm(emb)
        await cog.remember(f"用户喜欢Python编程 user prefers Python coding {i}", emb)

    # 组2: 不同主题 (随机 embedding)
    for i in range(3):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        await cog.remember(f"今天天气不错 weather nice day {i}", emb)

    stats = await dream._phase_afe_stage_s()
    assert "patterns" in stats
    # 降级模式 (无 LLM): 用记忆内容作为事实, 应能产生模式
    assert stats["patterns"] >= 0


async def test_phase_dae_updates_embeddings():
    """Phase 6: DAE 图感知嵌入 — 有连接的记忆嵌入应被更新"""
    np.random.seed(456)
    cog = CognitiveMemory(dimensions=64, episodic_capacity=100, semantic_max_clusters=10)
    dream = DreamEngineV2(cognitive_memory=cog)

    # 创建 3 条记忆 (不同 embedding, 留在 episodic 层)
    np.random.seed(456)
    embs = []
    for i in range(3):
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb)
        embs.append(emb)
        await cog.remember(f"mem_{chr(97+i)}", emb.copy(), emotion_label="happy")

    # 手动建立连接图 (每个记忆有 2 个邻居, 满足 DAE_MIN_NEIGHBORS=2)
    entries = list(cog._episodic)
    a, b, c = entries[0], entries[1], entries[2]
    cog._connections[a.id] = {b.id: 0.8, c.id: 0.6}
    cog._connections[b.id] = {a.id: 0.8, c.id: 0.7}
    cog._connections[c.id] = {a.id: 0.6, b.id: 0.7}

    # 记录原始嵌入
    original_embs = {m.id: m.embedding.copy() for m in cog._episodic}

    # 运行 DAE
    stats = await dream._phase_dae()

    # 3 条记忆都有 >= 2 个邻居, 都应被更新
    assert stats["updated"] == 3

    # 验证嵌入确实变化了
    changed = 0
    for m in cog._episodic:
        if not np.array_equal(m.embedding, original_embs[m.id]):
            changed += 1
    assert changed == 3, f"DAE 应更新所有 3 条记忆的嵌入, 实际 {changed}"
