"""FSRS-DSR 记忆模型 — 基于 Free Spaced Repetition Scheduler

核心公式: R(t) = e^(-t/S)
三变量模型: Difficulty / Stability / Retrievability
状态机: BUFFER → REINFORCED/DECAY → PERMANENT/ARCHIVED
"""
import math
import time
from dataclasses import dataclass, field
from enum import Enum


class MemoryPhase(Enum):
    BUFFER = "buffer"
    REINFORCED = "reinforced"
    DECAY = "decayed"
    PERMANENT = "permanent"
    ARCHIVED = "archived"


class ReinforcementSignal(Enum):
    STRONG_CONFIRM = "strong_confirm"
    PASSIVE_USE = "passive_use"
    WEAK_HIT = "weak_hit"
    CORRECT = "correct"

    @property
    def growth_factor(self) -> float:
        _map = {
            ReinforcementSignal.STRONG_CONFIRM: 2.0,
            ReinforcementSignal.PASSIVE_USE: 1.5,
            ReinforcementSignal.WEAK_HIT: 1.0,
            ReinforcementSignal.CORRECT: 0.0,
        }
        return _map[self]


S_PERMANENT = 30.0
R_ARCHIVE = 0.05
R_FORGET = 0.02
BUFFER_DAYS = 21
LOOKBACK_DAYS = 7
SIMILARITY_THRESHOLD = 0.6
S_INIT = 3.0
D_INIT = 5.0
D_MEAN = 5.0
MEAN_REVERT = 0.3
FORGET_THRESHOLD = 0.05
DREAM_THRESHOLD = 0.15

_FACT_KEYWORDS = ("生日", "电话", "地址", "名字", "日期", "号码")
_PREF_KEYWORDS = ("喜欢", "讨厌", "偏好", "习惯", "总是")
_ABSTRACT_KEYWORDS = ("因为", "所以", "意味着", "本质上", "原理")


@dataclass
class MemoryState:
    difficulty: float = D_INIT
    stability: float = S_INIT
    phase: MemoryPhase = MemoryPhase.BUFFER
    last_review: float = 0.0
    created_at: float = 0.0
    reinforcement_count: int = 0

    def retrievability(self, now: float | None = None) -> float:
        if self.phase == MemoryPhase.BUFFER:
            return 1.0
        if self.phase == MemoryPhase.PERMANENT:
            return 1.0
        if self.phase == MemoryPhase.ARCHIVED:
            return 0.0
        now = now or time.time()
        elapsed_days = max(0.0, (now - self.last_review) / 86400.0)
        return math.exp(-elapsed_days / self.stability)

    def transition(self, now: float | None = None) -> MemoryPhase:
        now = now or time.time()
        if self.phase == MemoryPhase.BUFFER:
            age_days = (now - self.created_at) / 86400.0
            if age_days <= BUFFER_DAYS:
                return MemoryPhase.BUFFER
            if self.reinforcement_count == 0:
                return MemoryPhase.DECAY
            if self.stability >= S_PERMANENT:
                return MemoryPhase.PERMANENT
            return MemoryPhase.REINFORCED
        if self.phase in (MemoryPhase.REINFORCED, MemoryPhase.DECAY):
            R = self.retrievability(now)
            if R < R_ARCHIVE:
                return MemoryPhase.ARCHIVED
            if self.stability >= S_PERMANENT:
                return MemoryPhase.PERMANENT
            return self.phase
        if self.phase == MemoryPhase.PERMANENT:
            return MemoryPhase.PERMANENT
        return self.phase


def estimate_initial_difficulty(content: str, emotion_label: str = "") -> float:
    D = D_INIT
    length = len(content)
    if length < 20:
        D -= 1.0
    elif length > 200:
        D += 1.5
    if emotion_label and emotion_label.lower() not in ("", "neutral", "中性"):
        D += 1.0
    if any(kw in content for kw in _FACT_KEYWORDS):
        D -= 2.0
    elif any(kw in content for kw in _PREF_KEYWORDS):
        D -= 1.0
    elif any(kw in content for kw in _ABSTRACT_KEYWORDS):
        D += 2.0
    return max(1.0, min(10.0, D))


class FSRSModel:
    def reinforce(self, state: MemoryState, signal: ReinforcementSignal,
                  now: float | None = None) -> MemoryState:
        now = now or time.time()
        if signal == ReinforcementSignal.CORRECT:
            return self._apply_forget(state, now)
        return self._apply_recall(state, signal, now)

    def _compute_phase(self, D: float, S: float, state: MemoryState,
                       now: float) -> MemoryPhase:
        age_days = (now - state.created_at) / 86400.0
        if S >= S_PERMANENT and state.reinforcement_count > 0 and age_days > BUFFER_DAYS:
            return MemoryPhase.PERMANENT
        elif state.reinforcement_count > 0 and age_days > BUFFER_DAYS:
            return MemoryPhase.REINFORCED
        elif age_days > BUFFER_DAYS:
            return MemoryPhase.DECAY
        else:
            return MemoryPhase.BUFFER

    def _apply_recall(self, state: MemoryState, signal: ReinforcementSignal,
                      now: float) -> MemoryState:
        R = state.retrievability(now)
        D = state.difficulty
        S = state.stability
        difficulty_factor = max(0.0, (10.0 - D) / 9.0)
        retrievability_bonus = 1.0 + 2.0 * (1.0 - R)
        growth = signal.growth_factor * difficulty_factor * retrievability_bonus
        S_new = min(S * (1.0 + growth), S * 10.0)
        D_new = self._update_difficulty(D, signal)
        rc = state.reinforcement_count + 1
        new_phase = self._compute_phase(D_new, S_new, state, now)
        return MemoryState(
            difficulty=D_new, stability=S_new,
            phase=new_phase, last_review=now,
            created_at=state.created_at,
            reinforcement_count=rc,
        )

    def _apply_forget(self, state: MemoryState, now: float) -> MemoryState:
        D = state.difficulty
        S = state.stability
        S_new = S_INIT * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)
        S_new = max(1.0, min(S_new, S))
        D_new = self._update_difficulty(D, ReinforcementSignal.CORRECT)
        new_phase = self._compute_phase(D_new, S_new, state, now)
        return MemoryState(
            difficulty=D_new, stability=S_new,
            phase=new_phase, last_review=now,
            created_at=state.created_at,
            reinforcement_count=state.reinforcement_count,
        )

    @staticmethod
    def _update_difficulty(D: float, signal: ReinforcementSignal) -> float:
        delta_map = {
            ReinforcementSignal.STRONG_CONFIRM: -0.5,
            ReinforcementSignal.PASSIVE_USE: -0.2,
            ReinforcementSignal.WEAK_HIT: 0.0,
            ReinforcementSignal.CORRECT: 1.0,
        }
        delta = delta_map[signal]
        D_new = MEAN_REVERT * D_MEAN + (1.0 - MEAN_REVERT) * (D + delta)
        return max(1.0, min(10.0, D_new))

    def should_filter(self, R: float) -> bool:
        return R < FORGET_THRESHOLD

    def should_archive(self, R: float) -> bool:
        return R < DREAM_THRESHOLD

    def score(self, similarity: float, state: MemoryState,
              now: float | None = None) -> float:
        R = state.retrievability(now)
        return similarity * R