import math
import time

import pytest

from memory.fsrs_model import (
    BUFFER_DAYS,
    D_INIT,
    D_MEAN,
    DREAM_THRESHOLD,
    FORGET_THRESHOLD,
    MEAN_REVERT,
    R_ARCHIVE,
    S_INIT,
    S_PERMANENT,
    FSRSModel,
    MemoryPhase,
    MemoryState,
    ReinforcementSignal,
    estimate_initial_difficulty,
)


class TestRetrievability:
    def test_buffer_returns_1(self):
        s = MemoryState(phase=MemoryPhase.BUFFER)
        assert s.retrievability() == 1.0

    def test_permanent_returns_1(self):
        s = MemoryState(phase=MemoryPhase.PERMANENT)
        assert s.retrievability() == 1.0

    def test_archived_returns_0(self):
        s = MemoryState(phase=MemoryPhase.ARCHIVED)
        assert s.retrievability() == 0.0

    def test_decay_formula(self):
        now = time.time()
        s = MemoryState(
            phase=MemoryPhase.DECAY,
            stability=10.0,
            last_review=now - 10 * 86400,
        )
        R = s.retrievability(now)
        expected = math.exp(-10.0 / 10.0)
        assert abs(R - expected) < 1e-9

    def test_higher_stability_slower_decay(self):
        now = time.time()
        s_low = MemoryState(phase=MemoryPhase.REINFORCED, stability=5.0, last_review=now - 10 * 86400)
        s_high = MemoryState(phase=MemoryPhase.REINFORCED, stability=50.0, last_review=now - 10 * 86400)
        assert s_high.retrievability(now) > s_low.retrievability(now)


class TestTransition:
    def test_buffer_stays_within_21_days(self):
        now = time.time()
        s = MemoryState(phase=MemoryPhase.BUFFER, created_at=now - 10 * 86400)
        assert s.transition(now) == MemoryPhase.BUFFER

    def test_buffer_to_decay_no_reinforcement(self):
        now = time.time()
        s = MemoryState(
            phase=MemoryPhase.BUFFER,
            created_at=now - 22 * 86400,
            reinforcement_count=0,
        )
        assert s.transition(now) == MemoryPhase.DECAY

    def test_buffer_to_reinforced(self):
        now = time.time()
        s = MemoryState(
            phase=MemoryPhase.BUFFER,
            created_at=now - 22 * 86400,
            reinforcement_count=1,
            stability=10.0,
        )
        assert s.transition(now) == MemoryPhase.REINFORCED

    def test_buffer_to_permanent(self):
        now = time.time()
        s = MemoryState(
            phase=MemoryPhase.BUFFER,
            created_at=now - 22 * 86400,
            reinforcement_count=5,
            stability=S_PERMANENT,
        )
        assert s.transition(now) == MemoryPhase.PERMANENT

    def test_decay_to_archived(self):
        now = time.time()
        s = MemoryState(
            phase=MemoryPhase.DECAY,
            stability=1.0,
            last_review=now - 100 * 86400,
        )
        assert s.retrievability(now) < R_ARCHIVE
        assert s.transition(now) == MemoryPhase.ARCHIVED

    def test_reinforced_to_permanent(self):
        s = MemoryState(
            phase=MemoryPhase.REINFORCED,
            stability=S_PERMANENT,
            last_review=time.time(),
        )
        assert s.transition() == MemoryPhase.PERMANENT


class TestReinforce:
    def test_stability_increases_on_confirm(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(stability=S_INIT, last_review=now - 86400, created_at=now - 86400)
        result = model.reinforce(s, ReinforcementSignal.STRONG_CONFIRM, now=now)
        assert result.stability > S_INIT

    def test_stability_decreases_on_correct(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(stability=10.0, difficulty=5.0, last_review=now - 86400, created_at=now - 86400)
        result = model.reinforce(s, ReinforcementSignal.CORRECT, now=now)
        assert result.stability < 10.0

    def test_last_review_updates(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(last_review=0.0, created_at=now)
        result = model.reinforce(s, ReinforcementSignal.PASSIVE_USE, now=now)
        assert result.last_review == now

    def test_reinforcement_count_increments_on_recall(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(reinforcement_count=0, created_at=now)
        result = model.reinforce(s, ReinforcementSignal.WEAK_HIT, now=now)
        assert result.reinforcement_count == 1

    def test_reinforcement_count_unchanged_on_correct(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(reinforcement_count=3, created_at=now)
        result = model.reinforce(s, ReinforcementSignal.CORRECT, now=now)
        assert result.reinforcement_count == 3

    def test_low_retrievability_gives_bigger_stability_boost(self):
        model = FSRSModel()
        now = time.time()
        s_fresh = MemoryState(
            phase=MemoryPhase.REINFORCED, stability=S_INIT,
            last_review=now - 86400, created_at=now - 86400,
        )
        s_old = MemoryState(
            phase=MemoryPhase.REINFORCED, stability=S_INIT,
            last_review=now - 30 * 86400, created_at=now - 30 * 86400,
        )
        result_fresh = model.reinforce(s_fresh, ReinforcementSignal.STRONG_CONFIRM, now=now)
        result_old = model.reinforce(s_old, ReinforcementSignal.STRONG_CONFIRM, now=now)
        assert result_old.stability > result_fresh.stability

    def test_difficulty_decreases_on_confirm(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(difficulty=7.0, created_at=now)
        result = model.reinforce(s, ReinforcementSignal.STRONG_CONFIRM, now=now)
        assert result.difficulty < 7.0

    def test_difficulty_increases_on_correct(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(difficulty=3.0, created_at=now)
        result = model.reinforce(s, ReinforcementSignal.CORRECT, now=now)
        assert result.difficulty > 3.0


class TestEstimateInitialDifficulty:
    def test_default_is_5(self):
        content = "这是一段普通长度的内容，长度超过二十个字符"
        assert estimate_initial_difficulty(content) == D_INIT

    def test_emotion_increases(self):
        d_neutral = estimate_initial_difficulty("普通内容", "neutral")
        d_emotional = estimate_initial_difficulty("普通内容", "happy")
        assert d_emotional > d_neutral

    def test_fact_decreases(self):
        d = estimate_initial_difficulty("我的生日是明天")
        assert d < D_INIT

    def test_abstract_increases(self):
        d = estimate_initial_difficulty("因为所以意味着本质上原理")
        assert d > D_INIT

    def test_clamped_to_1(self):
        d = estimate_initial_difficulty("生日电话地址", "neutral")
        assert d >= 1.0

    def test_clamped_to_10(self):
        d = estimate_initial_difficulty("因为所以意味着本质上原理这是一个非常长的内容" * 20, "angry")
        assert d <= 10.0

    def test_short_content_decreases(self):
        d = estimate_initial_difficulty("短")
        assert d < D_INIT

    def test_long_content_increases(self):
        d = estimate_initial_difficulty("x" * 300)
        assert d > D_INIT


class TestFSRSModelScore:
    def test_score_equals_similarity_times_R(self):
        model = FSRSModel()
        now = time.time()
        s = MemoryState(phase=MemoryPhase.REINFORCED, stability=10.0, last_review=now)
        R = s.retrievability(now)
        assert abs(model.score(0.8, s, now) - 0.8 * R) < 1e-9

    def test_old_memory_decays(self):
        model = FSRSModel()
        now = time.time()
        s_fresh = MemoryState(phase=MemoryPhase.REINFORCED, stability=10.0, last_review=now)
        s_old = MemoryState(phase=MemoryPhase.REINFORCED, stability=10.0, last_review=now - 30 * 86400)
        assert model.score(0.8, s_old, now) < model.score(0.8, s_fresh, now)


class TestThresholds:
    def test_should_filter(self):
        model = FSRSModel()
        assert model.should_filter(0.03) is True
        assert model.should_filter(0.06) is False

    def test_should_archive(self):
        model = FSRSModel()
        assert model.should_archive(0.10) is True
        assert model.should_archive(0.20) is False

    def test_constant_values(self):
        assert S_PERMANENT == 30.0
        assert R_ARCHIVE == 0.05
        assert BUFFER_DAYS == 21
        assert S_INIT == 3.0
        assert D_INIT == 5.0
        assert FORGET_THRESHOLD == 0.05
        assert DREAM_THRESHOLD == 0.15