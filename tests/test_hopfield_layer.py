# tests/test_hopfield_layer.py
"""Modern Hopfield 联想记忆测试"""
import numpy as np

from memory.hopfield_layer import HopfieldLayer


def test_store_and_retrieve():
    """测试存储和检索"""
    hop = HopfieldLayer(dimensions=64, capacity=10)
    pattern = np.random.randn(64).astype(np.float32)
    pattern /= np.linalg.norm(pattern)

    pid = hop.store(pattern, label="test1")
    assert pid > 0

    # 用完全相同的pattern检索 → 高confidence
    result = hop.retrieve(pattern)
    assert result.confidence > 0.9
    assert result.converged

def test_retrieve_with_noise():
    """测试带噪声检索: 能收敛回原始模式"""
    hop = HopfieldLayer(dimensions=64, capacity=10)
    pattern = np.random.randn(64).astype(np.float32)
    pattern /= np.linalg.norm(pattern)
    hop.store(pattern, label="test2")

    # 加少量噪声
    noisy = pattern + np.random.randn(64).astype(np.float32) * 0.1
    noisy /= np.linalg.norm(noisy)

    result = hop.retrieve(noisy)
    assert result.confidence > 0.8

def test_capacity_eviction():
    """测试容量满时驱逐最低salience"""
    hop = HopfieldLayer(dimensions=32, capacity=3)
    for i in range(5):
        p = np.random.randn(32).astype(np.float32)
        p /= np.linalg.norm(p)
        hop.store(p, label=f"pattern_{i}")
    assert hop.pattern_count() == 3  # 不超过capacity

def test_lookup_single_iteration():
    """测试单次迭代lookup"""
    hop = HopfieldLayer(dimensions=64, capacity=10)
    pattern = np.random.randn(64).astype(np.float32)
    pattern /= np.linalg.norm(pattern)
    hop.store(pattern)

    result = hop.lookup(pattern)
    assert result.confidence > 0.9
    assert result.iterations == 1

def test_empty_retrieve():
    """测试空库检索"""
    hop = HopfieldLayer(dimensions=64, capacity=10)
    cue = np.random.randn(64).astype(np.float32)
    result = hop.retrieve(cue)
    # 空库应返回cue本身, confidence=0
    assert result.confidence == 0.0

def test_update_salience():
    """测试salience衰减和更新"""
    hop = HopfieldLayer(dimensions=32, capacity=10)
    p = np.random.randn(32).astype(np.float32)
    p /= np.linalg.norm(p)
    hop.store(p)
    # 衰减后salience应降低
    hop.update_salience()
    assert hop.pattern_count() == 1
