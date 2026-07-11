# tests/test_conflict_supersession.py
"""冲突超驱测试"""
import asyncio
import re
import time
import numpy as np
import pytest
from core.conflict_supersession import ConflictSupersession, ConflictPair
from memory.cognitive_memory import MemoryEntry

@pytest.fixture
def cs():
    return ConflictSupersession()

def test_extract_numeric_tokens(cs):
    """测试数值token提取"""
    tokens = cs._extract_numeric_tokens("用户工资是5000元，房租1500")
    assert "5000" in tokens
    assert "1500" in tokens

def test_no_conflict_different_content(cs):
    """测试不同内容的记忆无冲突"""
    now = time.time()
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    m1 = MemoryEntry(id=1, content="用户喜欢猫", embedding=emb, timestamp=now)
    m2 = MemoryEntry(id=2, content="今天天气很好", embedding=emb, timestamp=now+1)
    conflicts = asyncio.get_event_loop().run_until_complete(
        cs.detect_conflicts([m1, m2])
    )
    assert len(conflicts) == 0

def test_conflict_same_topic_different_numbers(cs):
    """测试同主题不同数值=冲突"""
    now = time.time()
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    m1 = MemoryEntry(id=1, content="用户工资是5000元", embedding=emb, timestamp=now)
    m2 = MemoryEntry(id=2, content="用户工资是8000元", embedding=emb, timestamp=now+100)
    conflicts = asyncio.get_event_loop().run_until_complete(
        cs.detect_conflicts([m1, m2])
    )
    assert len(conflicts) == 1
    assert conflicts[0].old_memory_id == 1  # 旧的是m1
    assert conflicts[0].new_memory_id == 2  # 新的是m2

def test_no_conflict_same_numbers(cs):
    """测试同主题同数值=无冲突"""
    now = time.time()
    emb = np.random.randn(64).astype(np.float32)
    emb /= np.linalg.norm(emb)
    m1 = MemoryEntry(id=1, content="用户工资是5000元", embedding=emb, timestamp=now)
    m2 = MemoryEntry(id=2, content="用户月薪5000元", embedding=emb, timestamp=now+100)
    conflicts = asyncio.get_event_loop().run_until_complete(
        cs.detect_conflicts([m1, m2])
    )
    assert len(conflicts) == 0
