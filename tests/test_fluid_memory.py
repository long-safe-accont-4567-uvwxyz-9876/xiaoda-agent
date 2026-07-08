"""流体记忆系统单元测试"""
import math
import time

import pytest

from memory.fluid_memory import FluidMemory


# ── score 计算 ──


def test_score_new_memory():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.8
    score = fm.score(similarity=similarity, created_at=now, access_count=0)
    # 新记忆 days_passed≈0, decay≈1, boost=0
    # score ≈ similarity × 1 + 0 = similarity
    assert score == pytest.approx(similarity, abs=0.01)


def test_score_old_memory_decay():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.8
    # 100 天前的记忆
    old_time = now - 100 * 86400
    score = fm.score(similarity=similarity, created_at=old_time, access_count=0)
    # 衰减明显，分数应远低于 similarity
    assert score < similarity * 0.5


def test_score_access_boost():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.5
    score_low_access = fm.score(similarity=similarity, created_at=now, access_count=1)
    score_high_access = fm.score(similarity=similarity, created_at=now, access_count=100)
    # 高访问次数分数更高
    assert score_high_access > score_low_access


def test_score_formula_exact():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.9
    created_at = now - 10 * 86400  # 10 天前
    access_count = 5

    days_passed = (now - created_at) / 86400.0
    expected_decay = math.exp(-FluidMemory.LAMBDA_DECAY * days_passed)
    expected_boost = FluidMemory.ALPHA_BOOST * math.log(1 + access_count)
    expected_score = similarity * expected_decay + expected_boost

    score = fm.score(similarity=similarity, created_at=created_at, access_count=access_count)
    assert score == pytest.approx(expected_score, rel=1e-6)


# ── should_filter / should_archive ──


def test_should_filter_low_score():
    fm = FluidMemory()
    assert fm.should_filter(0.01) is True


def test_should_not_filter_high_score():
    fm = FluidMemory()
    assert fm.should_filter(0.5) is False


def test_should_archive_medium_score():
    fm = FluidMemory()
    # 分数低于 DREAM_THRESHOLD(0.15) 但高于 FORGET_THRESHOLD(0.05)
    assert fm.should_archive(0.10) is True


def test_should_not_archive_high_score():
    fm = FluidMemory()
    assert fm.should_archive(0.5) is False


# ── 常量值验证 ──


def test_forget_threshold_value():
    assert FluidMemory.FORGET_THRESHOLD == 0.05


def test_dream_threshold_value():
    assert FluidMemory.DREAM_THRESHOLD == 0.15


def test_lambda_decay_value():
    assert FluidMemory.LAMBDA_DECAY == 0.05


def test_alpha_boost_value():
    assert FluidMemory.ALPHA_BOOST == 0.2
