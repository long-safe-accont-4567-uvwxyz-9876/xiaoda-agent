"""P0-01 + P0-02: FSRS forget formula fix — tests"""
from __future__ import annotations

import time

from memory.fsrs_model import S_INIT, FSRSModel, MemoryPhase, MemoryState, ReinforcementSignal


def test_forget_decreases_stability():
    fsrs = FSRSModel()
    now = time.time()
    state = MemoryState(
        difficulty=5.0, stability=300.0,
        phase=MemoryPhase.REINFORCED,
        last_review=now - 86400, created_at=now - 30 * 86400,
        reinforcement_count=3,
    )
    result = fsrs.reinforce(state, ReinforcementSignal.CORRECT, now=now)
    assert result.stability < 300.0, f"S should decrease after forget, got {result.stability}"


def test_forget_uses_s_init_not_s():
    fsrs = FSRSModel()
    now = time.time()
    state = MemoryState(
        difficulty=5.0, stability=300.0,
        phase=MemoryPhase.REINFORCED,
        last_review=now - 86400, created_at=now - 30 * 86400,
        reinforcement_count=3,
    )
    result = fsrs.reinforce(state, ReinforcementSignal.CORRECT, now=now)
    expected_approx = S_INIT * (5.0 ** (-0.3)) * (((300.0 + 1.0) ** 0.2) - 1.0)
    assert abs(result.stability - expected_approx) < 1.0, f"Expected ~{expected_approx}, got {result.stability}"


def test_recall_reinforcement_count_exactly_plus_one():
    fsrs = FSRSModel()
    now = time.time()
    state = MemoryState(
        difficulty=5.0, stability=3.0,
        phase=MemoryPhase.BUFFER,
        last_review=now, created_at=now,
        reinforcement_count=5,
    )
    result = fsrs.reinforce(state, ReinforcementSignal.STRONG_CONFIRM, now=now)
    assert result.reinforcement_count == 6, f"Expected 6, got {result.reinforcement_count}"


def test_forget_stability_capped_at_current_s():
    fsrs = FSRSModel()
    now = time.time()
    state = MemoryState(
        difficulty=1.0, stability=2.0,
        phase=MemoryPhase.REINFORCED,
        last_review=now - 86400, created_at=now - 30 * 86400,
        reinforcement_count=1,
    )
    result = fsrs.reinforce(state, ReinforcementSignal.CORRECT, now=now)
    assert result.stability <= 2.0, f"S_new should be <= S_old after forget, got {result.stability}"
