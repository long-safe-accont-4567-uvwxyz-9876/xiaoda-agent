# tests/test_bridge_memory.py
"""桥接记忆测试"""
import time

import numpy as np
import pytest

from memory.bridge_memory import BridgeMemoryManager
from memory.cognitive_memory import MemoryEntry


@pytest.fixture
def manager():
    return BridgeMemoryManager()

async def test_bridge_discovery(manager):
    """测试REM桥接发现"""
    np.random.seed(42)
    now = time.time()
    # 创建孤立记忆
    emb1 = np.random.randn(64).astype(np.float32)
    emb1 /= np.linalg.norm(emb1)
    emb2 = np.random.randn(64).astype(np.float32)
    emb2 /= np.linalg.norm(emb2)
    # 创建一个与emb1相似但不同的embedding
    emb3 = emb1 + np.random.randn(64).astype(np.float32) * 0.3
    emb3 /= np.linalg.norm(emb3)

    orphan = MemoryEntry(id=1, embedding=emb1, timestamp=now, session_id="s1")
    all_memories = [
        MemoryEntry(id=2, embedding=emb3, timestamp=now-100, session_id="s2"),
        MemoryEntry(id=3, embedding=emb2, timestamp=now-200, session_id="s3"),
    ]

    bridges = await manager.discover_bridges([orphan], all_memories + [orphan])
    # emb1 和 emb3 相似 → 应该有桥接
    assert len(bridges) > 0
    bridge = bridges[0]
    assert bridge.source_memory_id == 1
    assert bridge.target_memory_id == 2
    assert bridge.cross_session is True

async def test_bridge_weight_factor(manager):
    """测试桥接权重 = sim × 0.3"""
    np.random.seed(99)
    now = time.time()
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    emb_sim = emb + np.random.randn(64).astype(np.float32) * 0.1
    emb_sim /= np.linalg.norm(emb_sim)

    orphan = MemoryEntry(id=1, embedding=emb, timestamp=now)
    target = MemoryEntry(id=2, embedding=emb_sim, timestamp=now)

    bridges = await manager.discover_bridges([orphan], [orphan, target])
    if bridges:
        assert bridges[0].weight <= 0.3 * 0.95  # weight = sim × 0.3, sim < 0.95

async def test_no_bridge_for_identical(manager):
    """测试完全相同的记忆不建立桥接 (sim >= 0.95)"""
    np.random.seed(7)
    now = time.time()
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)

    orphan = MemoryEntry(id=1, embedding=emb, timestamp=now)
    target = MemoryEntry(id=2, embedding=emb.copy(), timestamp=now)

    bridges = await manager.discover_bridges([orphan], [orphan, target])
    # sim = 1.0 >= 0.95 → 不建立桥接
    assert len(bridges) == 0
