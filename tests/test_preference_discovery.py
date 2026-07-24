# tests/test_preference_discovery.py
"""偏好发现测试"""
import numpy as np
import pytest

from memory.preference_discovery import PreferenceDiscovery


@pytest.fixture
def pd():
    return PreferenceDiscovery()

def test_cluster_outputs(pd):
    """测试Stage C输出聚类"""
    outputs = [
        "user prefers concise replies",
        "user likes short answers",
        "user enjoys late night chats",
        "user prefers brief responses",
        "user likes midnight conversations",
    ]
    embeddings = np.random.randn(5, 64).astype(np.float32)
    # 前3条相似, 后2条相似
    embeddings[0] = embeddings[1] = embeddings[3]  # concise/brief
    embeddings[2] = embeddings[4]  # night chats

    clusters = pd._cluster_by_similarity(outputs, embeddings, threshold=0.85)
    assert len(clusters) >= 1

def test_pattern_salience(pd):
    """测试模式salience=2.0"""
    assert pd.PATTERN_SALIENCE == 2.0
