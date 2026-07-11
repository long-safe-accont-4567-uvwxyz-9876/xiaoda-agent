"""流体记忆系统单元测试 — mind 风格 Ebbinghaus 增量模型"""
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
    # 新记忆 days≈0, retention≈1, weight=peak_weight×1=1
    # score ≈ similarity × 1.0 × 1 = similarity
    assert score == pytest.approx(similarity, abs=0.01)


def test_score_old_memory_decay():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.8
    # 100 天前的记忆，无确认
    old_time = now - 100 * 86400
    score = fm.score(similarity=similarity, created_at=old_time, access_count=0)
    # stability = 3.0, retention = e^(-100/3) ≈ 0
    assert score < similarity * 0.05


def test_score_access_boost():
    """确认次数影响稳定性（半衰期），而非加法 boost"""
    fm = FluidMemory()
    now = time.time()
    similarity = 0.5
    # 30 天前的记忆
    old_time = now - 30 * 86400
    score_low_access = fm.score(similarity=similarity, created_at=old_time,
                                 access_count=0)
    score_high_access = fm.score(similarity=similarity, created_at=old_time,
                                  access_count=10)
    # 10次确认: stability = 3 + 14×10 = 143 天, retention ≈ e^(-30/143) ≈ 0.81
    # 0次确认: stability = 3 天, retention ≈ e^(-30/3) ≈ 0
    assert score_high_access > score_low_access


def test_score_formula_exact():
    fm = FluidMemory()
    now = time.time()
    similarity = 0.9
    created_at = now - 10 * 86400  # 10 天前
    access_count = 5
    peak_weight = 0.8

    days_passed = (now - created_at) / 86400.0
    stability = (FluidMemory.STABILITY_BASE_DAYS
                 + access_count * FluidMemory.STABILITY_PER_ACCESS)
    retention = math.exp(-days_passed / stability)
    expected_score = similarity * peak_weight * retention

    score = fm.score(similarity=similarity, created_at=created_at,
                     access_count=access_count, peak_weight=peak_weight)
    assert score == pytest.approx(expected_score, rel=1e-6)


def test_confirmed_memory_retention():
    """10次确认的记忆 30 天后保留率 ≥ 80%"""
    fm = FluidMemory()
    now = time.time()
    created_at = now - 30 * 86400
    score = fm.score(similarity=1.0, created_at=created_at,
                     access_count=10, peak_weight=1.0)
    # stability = 3 + 14×10 = 143, retention = e^(-30/143) ≈ 0.811
    assert score >= 0.80


def test_peak_weight_affects_score():
    fm = FluidMemory()
    now = time.time()
    score_default = fm.score(similarity=0.8, created_at=now, access_count=0)
    score_high_peak = fm.score(similarity=0.8, created_at=now, access_count=0,
                                peak_weight=1.5)
    assert score_high_peak > score_default


def test_no_max_boost_cap():
    """新模型无 MAX_BOOST 硬上限：高确认记忆分数随确认次数增长"""
    fm = FluidMemory()
    now = time.time()
    created_at = now - 5 * 86400  # 5 天前
    score_5 = fm.score(similarity=0.5, created_at=created_at, access_count=5)
    score_50 = fm.score(similarity=0.5, created_at=created_at, access_count=50)
    # 50次确认的稳定性远高于5次，分数更高
    assert score_50 > score_5


# ── should_filter / should_archive ──


def test_should_filter_low_score():
    fm = FluidMemory()
    assert fm.should_filter(0.01) is True


def test_should_not_filter_high_score():
    fm = FluidMemory()
    assert fm.should_filter(0.5) is False


def test_should_archive_medium_score():
    fm = FluidMemory()
    assert fm.should_archive(0.10) is True


def test_should_not_archive_high_score():
    fm = FluidMemory()
    assert fm.should_archive(0.5) is False


# ── 常量值验证 ──


def test_forget_threshold_value():
    assert FluidMemory.FORGET_THRESHOLD == 0.05


def test_dream_threshold_value():
    assert FluidMemory.DREAM_THRESHOLD == 0.15


def test_stability_base_days_value():
    assert FluidMemory.STABILITY_BASE_DAYS == 3.0


def test_stability_per_access_value():
    assert FluidMemory.STABILITY_PER_ACCESS == 14.0


def test_boost_per_access_value():
    assert FluidMemory.BOOST_PER_ACCESS == 0.15


def test_grace_days_value():
    assert FluidMemory.GRACE_DAYS == 45


def test_weight_threshold_value():
    assert FluidMemory.WEIGHT_THRESHOLD == 0.1


def test_no_lambda_decay_attribute():
    """旧参数应已移除"""
    assert not hasattr(FluidMemory, "LAMBDA_DECAY")


def test_no_alpha_boost_attribute():
    assert not hasattr(FluidMemory, "ALPHA_BOOST")


def test_no_max_boost_attribute():
    assert not hasattr(FluidMemory, "MAX_BOOST")
