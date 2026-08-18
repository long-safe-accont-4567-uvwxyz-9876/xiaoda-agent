# tests/test_salience.py
"""SalienceScorer 情绪加权评分测试"""
import time
from dataclasses import dataclass

from memory.salience import SalienceScorer


@dataclass
class MockEntry:
    """模拟记忆条目"""
    access_count: int = 0
    last_accessed: float = 0.0
    created_at: float = 0.0
    emotion_label: str = ""
    embedding: list = None

@dataclass
class MockPAD:
    """模拟PAD状态"""
    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0
    dominant_emotion: str = "neutral"

def test_recency_score():
    """测试时近性评分: 1小时半衰期"""
    scorer = SalienceScorer()
    now = time.time()
    # 1秒前访问 → recency_score ≈ exp(-1/3600) ≈ 0.9997
    entry = MockEntry(access_count=0, last_accessed=now-1, created_at=now-1)
    score = scorer.compute(entry, now)
    assert score > 0.3  # recency 高

def test_frequency_score():
    """测试频率评分: log1p(access)/10"""
    scorer = SalienceScorer()
    now = time.time()
    entry = MockEntry(access_count=10, last_accessed=now, created_at=now)
    score = scorer.compute(entry, now)
    # freq_score = min(log1p(10)/10, 1.0) = min(2.398/10, 1.0) = 0.2398
    # 纯freq贡献 = 0.3 * 0.2398 = 0.072
    assert score > 0.0

def test_emotion_score_with_pad():
    """测试情绪加权: 高arousal提升评分"""
    scorer = SalienceScorer()
    now = time.time()
    entry = MockEntry(access_count=1, last_accessed=now, created_at=now, emotion_label="happy")
    # 无PAD → emotion_score = 0.5
    score_no_pad = scorer.compute(entry, now, pad_state=None)
    # 有PAD, arousal=0.8, dominant_emotion="happy" → emotion_score高
    pad = MockPAD(arousal=0.8, dominant_emotion="happy")
    score_with_pad = scorer.compute(entry, now, pad_state=pad)
    assert score_with_pad > score_no_pad

def test_emotion_label_match():
    """测试情绪标签匹配: 同标签加成"""
    scorer = SalienceScorer()
    now = time.time()
    entry = MockEntry(access_count=1, last_accessed=now, created_at=now, emotion_label="sad")
    # 匹配标签
    pad_match = MockPAD(arousal=0.5, dominant_emotion="sad")
    # 不匹配标签
    pad_no_match = MockPAD(arousal=0.5, dominant_emotion="happy")
    score_match = scorer.compute(entry, now, pad_state=pad_match)
    score_no_match = scorer.compute(entry, now, pad_state=pad_no_match)
    assert score_match > score_no_match

def test_old_memory_low_score():
    """测试旧记忆低分: 30天前"""
    scorer = SalienceScorer()
    now = time.time()
    entry = MockEntry(access_count=0, last_accessed=now-86400*30, created_at=now-86400*30)
    score = scorer.compute(entry, now)
    # recency_score = exp(-30*86400/3600) ≈ 0
    assert score < 0.1
