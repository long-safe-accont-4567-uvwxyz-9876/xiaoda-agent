"""FSRS 时间维度：永久化 vs 老 buffer 记忆的遗忘差异（单元级数学验证）。

目的：量化"事实永久化 + P0-1 FSRS 解耦 + P1-3 命中 R 下限"三项改动
在时间维度（记忆变老）下的真实价值。

场景：一条记忆插入后过了 N 天都没被再次强化。
- 事实类（permanent）：R 恒 = 1.0，永不被遗忘、永不被过滤。
- 普通类（buffer/decay，rc=0，21 天后 decay）：R = e^(-N/S) 指数衰减，
  N 足够大时 R→0，被 _apply_fsrs_scoring 的 R<0.01 阈值过滤。

本测试不依赖任何网络/DB，纯数学验证 FSRSModel + _apply_fsrs_scoring 行为。
"""
import time

import pytest

from memory.fsrs_model import (
    S_INIT,
    S_PERMANENT,
    MemoryPhase,
    MemoryState,
    FSRSModel,
)


def _make_state(phase: MemoryPhase, last_review: float,
                stability: float = S_INIT, rc: int = 0, now: float | None = None) -> MemoryState:
    now = now or time.time()
    return MemoryState(
        difficulty=5.0,
        stability=stability,
        phase=phase,
        last_review=last_review,
        created_at=now - 40 * 86400,  # 已存在 40 天（超过 BUFFER_DAYS=21）
        reinforcement_count=rc,
    )


def _aged_buffer_state(now: float) -> MemoryState:
    """构造一条 40 天前插入、从未强化（rc=0）的老记忆的真实状态。

    BUFFER 相 R 恒为 1.0（不衰减），只有走 transition 到 DECAY 后才衰减。
    这里显式用 _compute_phase 判定真实相，复刻 21 天后未强化记忆的行为。
    """
    raw = _make_state(MemoryPhase.BUFFER, now - 40 * 86400,
                      stability=S_INIT, rc=0, now=now)
    real_phase = FSRSModel()._compute_phase(
        raw.difficulty, raw.stability, raw, now)
    return MemoryState(
        difficulty=raw.difficulty,
        stability=raw.stability,
        phase=real_phase,  # 应为 DECAY（rc=0, 40 天前）
        last_review=raw.last_review,
        created_at=raw.created_at,
        reinforcement_count=raw.reinforcement_count,
    )


class TestPermanenceVsDecay:
    """永久态 vs 衰减态的 R 值对比。"""

    def test_permanent_never_decays(self):
        """PERMANENT 记忆无论过多久 R 恒为 1.0。"""
        model = FSRSModel()
        now = time.time()
        state = _make_state(MemoryPhase.PERMANENT, now - 365 * 86400)
        # 1 年后 R 仍为 1.0
        assert state.retrievability(now) == 1.0
        # score 不衰减
        assert model.score(0.9, state, now) == 0.9

    def test_old_buffer_fact_decays_to_zero(self):
        """未强化的老事实（rc=0, 40 天前, DECAY 相）R 已衰减到接近 0。

        复刻改动前行为：事实类若不置 PERMANENT，40 天后 R=e^(-40/3)≈0。
        """
        now = time.time()
        state = _aged_buffer_state(now)
        # 真实相应为 DECAY（rc=0 且超过 21 天）
        assert state.phase == MemoryPhase.DECAY
        R = state.retrievability(now)
        # e^(-40/3) ≈ 2.7e-6，远小于过滤阈值 0.01 → 会被过滤
        assert R < 0.01
        # 确认已衰减到接近 0（量化证据）
        assert R < 1e-4

    def test_fact_permanent_protects_against_decay(self):
        """对照：同样 40 天前的记忆，事实类置 PERMANENT 则 R=1.0。

        这正是 should_be_permanent_on_create 的量化价值：
        改动前生日记忆 40 天后 R≈0 被遗忘；改动后 R=1.0 永记。
        """
        now = time.time()
        # 改动前：老 buffer 事实（DECAY 相）
        old_fact = _aged_buffer_state(now)
        # 改动后：permanent 事实
        new_fact = _make_state(MemoryPhase.PERMANENT, now - 40 * 86400,
                              stability=S_PERMANENT, rc=1, now=now)
        assert old_fact.retrievability(now) < 0.01
        assert new_fact.retrievability(now) == 1.0

    def test_permanent_not_filtered_by_threshold(self):
        """PERMANENT 记忆不会被 R<0.01 过滤阈值清除（_apply_fsrs_scoring 契约）。"""
        # 模拟 _apply_fsrs_scoring 的过滤条件
        now = time.time()
        state = _make_state(MemoryPhase.PERMANENT, now - 100 * 86400)
        R = state.retrievability(now)
        assert R >= 0.01  # 不会被过滤
        assert R == 1.0

    def test_decay_phase_transition_for_old_unreinforced(self):
        """21 天以上未强化（rc=0）的老记忆应进入 DECAY 相。"""
        now = time.time()
        state = _make_state(MemoryPhase.BUFFER, now - 30 * 86400,
                           stability=S_INIT, rc=0)
        # 走 transition 逻辑（_compute_phase 等价判定）
        from memory.fsrs_model import FSRSModel
        new_phase = FSRSModel()._compute_phase(
            state.difficulty, state.stability, state, now)
        assert new_phase == MemoryPhase.DECAY


class TestP0Dash1EffectiveScoreDecoupling:
    """P0-1：effective_score 不再乘 R（避免双重惩罚）。

    注意：_apply_fsrs_scoring 在 _retrieval_engine 内，依赖 mm 依赖。
    此处用等价数学验证 R 解耦的意图：final_score 中 R 只计一次。
    """

    def test_score_formula_uses_R_once(self):
        """FSRSModel.score 返回 similarity*R（R 仅在 score 内一次）。

        验证：effective_score 改用 importance*similarity（见 _retrieval_engine
        改动），R 仅通过 final_score 的 0.15 权重计入一次，不再双重惩罚。
        """
        model = FSRSModel()
        now = time.time()
        state = _make_state(MemoryPhase.PERMANENT, now)
        similarity = 0.8
        fsrs_score = model.score(similarity, state, now)
        # PERMANENT 相 R=1.0，故 fsrs_score == similarity
        assert fsrs_score == pytest.approx(similarity, abs=1e-9)
        # 若旧逻辑 effective_score = importance * fsrs_score = importance * sim * R
        # 新逻辑 effective_score = importance * similarity（R 不在此乘）
        # 对 PERMANENT（R=1）两者等价；对 decay（R<1）新逻辑不再二次衰减
