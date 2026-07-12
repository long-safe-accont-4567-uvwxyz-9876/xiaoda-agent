# FSRS-DSR 记忆系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 FSRS-DSR 三变量模型 (Difficulty/Stability/Retrievability) 替代现有 4 套冲突的评分体系，实现统一的记忆生命周期管理。

**Architecture:** 新建 `memory/fsrs_model.py` 作为核心算法模块，包含 MemoryPhase 枚举、ReinforcementSignal 枚举、MemoryState 数据类和 FSRSModel 类。修改 memory_manager.py 的评分管线（_apply_fluid_scoring → _apply_fsrs_scoring, 删除 _compute_recency_boost, 更新 _compute_final_scores）。修改 confirm_correct.py 用 S/D 更新替代 weight+0.15。修改 dream_consolidation.py 用 R 判断归档。数据库增加 v15 迁移添加 5 列。

**Tech Stack:** Python 3.11+, aiosqlite, loguru, pytest-asyncio

## Global Constraints

- 数据库迁移必须幂等（先 PRAGMA table_info 检查列是否存在再 ALTER TABLE）
- 所有新列必须有 DEFAULT 值，旧数据自动获得 D=5.0, S=3.0, phase='buffer'
- 旧数据中 access_count >= 5 的直接标记为 phase='permanent'
- FSRS 常量: S_PERMANENT=30.0, R_ARCHIVE=0.05, R_FORGET=0.02, BUFFER_DAYS=21, LOOKBACK_DAYS=7, SIMILARITY_THRESHOLD=0.6, S_INIT=3.0, D_INIT=5.0
- 统一评分公式: final = 0.5×rerank + 0.3×R + 0.1×kg + 0.1×importance
- 情绪维度通过 D 初始化融入 FSRS: emotion_label 非空时 D += 1
- SalienceScorer 降级为 CognitiveMemory 内部 consolidation 决策辅助

---

## File Structure

| 文件 | 职责 | 变更类型 |
|------|------|----------|
| `memory/fsrs_model.py` | FSRS-DSR 核心算法: MemoryPhase, ReinforcementSignal, MemoryState, FSRSModel | 新建 |
| `memory/fluid_memory.py` | 旧 FluidMemory — 保留但标记废弃，添加 re-export 兼容层 | 修改 |
| `memory/memory_manager.py` | 评分管线: _apply_fsrs_scoring, 删除 _compute_recency_boost, 更新 _compute_final_scores | 修改 |
| `memory/confirm_correct.py` | confirm 更新 S 和 D，correct 继承 S/D | 修改 |
| `memory/concept_graph.py` | remember() 传入 D 初始化参数 | 修改 |
| `core/dream_consolidation.py` | 用 FSRSModel 替代 FluidMemory | 修改 |
| `db/database.py` | v15 迁移: episodic_memories +5 列, concept_nodes +5 列 | 修改 |
| `db/db_concept.py` | insert_node 增加 difficulty/stability/phase/reinforcement_count/last_review 参数 | 修改 |
| `db/db_memory.py` | 新增 update_fsrs_state, get_memories_since 方法 | 修改 |
| `tests/test_fsrs_model.py` | FSRSModel 单元测试 | 新建 |
| `tests/test_fluid_memory.py` | 迁移到 FSRS 测试 | 修改 |

---

### Task 1: 创建 fsrs_model.py 核心算法模块

**Files:**
- Create: `memory/fsrs_model.py`
- Test: `tests/test_fsrs_model.py`

**Interfaces:**
- Produces: `MemoryPhase` 枚举, `ReinforcementSignal` 枚举, `MemoryState` 数据类, `FSRSModel` 类

- [ ] **Step 1: 创建 fsrs_model.py 核心模块**

```python
# memory/fsrs_model.py
"""FSRS-DSR 记忆模型 — 基于人类记忆行为的遗忘曲线

参考: FSRS (Free Spaced Repetition Scheduler), Jarrett Ye, ACM SIGKDD 2022
DSR 三变量: Difficulty / Stability / Retrievability

核心公式: R(t) = e^(-t/S)
"""
from __future__ import annotations

import math
import time
from enum import Enum
from dataclasses import dataclass, field


class MemoryPhase(Enum):
    BUFFER = "buffer"
    REINFORCED = "reinforced"
    DECAY = "decay"
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


@dataclass
class MemoryState:
    difficulty: float = D_INIT
    stability: float = S_INIT
    phase: MemoryPhase = MemoryPhase.BUFFER
    last_review: float = 0.0
    created_at: float = 0.0
    reinforcement_count: int = 0

    def retrievability(self, now: float | None = None) -> float:
        if now is None:
            now = time.time()
        if self.phase == MemoryPhase.BUFFER:
            return 1.0
        if self.phase == MemoryPhase.PERMANENT:
            return 1.0
        if self.phase == MemoryPhase.ARCHIVED:
            return 0.0
        if self.stability <= 0:
            return 0.0
        elapsed_days = max(0, (now - self.last_review) / 86400.0)
        return math.exp(-elapsed_days / self.stability)

    def transition(self, now: float | None = None) -> MemoryPhase:
        if now is None:
            now = time.time()
        age_days = (now - self.created_at) / 86400.0

        if self.phase == MemoryPhase.BUFFER:
            if age_days > BUFFER_DAYS:
                if self.reinforcement_count == 0:
                    return MemoryPhase.DECAY
                elif self.stability >= S_PERMANENT:
                    return MemoryPhase.PERMANENT
                else:
                    return MemoryPhase.REINFORCED
            return MemoryPhase.BUFFER

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
    if emotion_label and emotion_label not in ("neutral", ""):
        D += 1.0
    fact_kw = ["生日", "电话", "地址", "名字", "日期", "号码"]
    pref_kw = ["喜欢", "讨厌", "偏好", "习惯", "总是"]
    abst_kw = ["因为", "所以", "意味着", "本质上", "原理"]
    if any(k in content for k in fact_kw):
        D -= 2.0
    elif any(k in content for k in pref_kw):
        D -= 1.0
    elif any(k in content for k in abst_kw):
        D += 2.0
    return max(1.0, min(10.0, D))


class FSRSModel:
    """FSRS-DSR 记忆模型

    用法:
        model = FSRSModel()
        state = MemoryState(created_at=time.time(), last_review=time.time())
        R = state.retrievability()
        model.reinforce(state, ReinforcementSignal.PASSIVE_USE)
    """

    def reinforce(self, state: MemoryState, signal: ReinforcementSignal,
                  now: float | None = None) -> None:
        if now is None:
            now = time.time()
        if signal == ReinforcementSignal.CORRECT:
            self._apply_forget(state, now)
            return
        self._apply_recall(state, signal, now)

    def _apply_recall(self, state: MemoryState, signal: ReinforcementSignal,
                      now: float) -> None:
        R = state.retrievability(now)
        difficulty_factor = (10.0 - state.difficulty) / 9.0
        retrievability_bonus = 1.0 + 2.0 * (1.0 - R)
        growth = signal.growth_factor * difficulty_factor * retrievability_bonus
        state.stability = min(state.stability * (1.0 + growth), state.stability * 10.0)
        state.difficulty = self._update_difficulty(state.difficulty, signal)
        state.last_review = now
        state.reinforcement_count += 1
        new_phase = state.transition(now)
        if new_phase != state.phase:
            state.phase = new_phase

    def _apply_forget(self, state: MemoryState, now: float) -> None:
        R = state.retrievability(now)
        regress = 0.5
        d_power = 0.3
        s_alpha = 0.2
        S = state.stability
        D = state.difficulty
        S_new = S * regress * (D ** (-d_power)) * (((S + 1) ** s_alpha) - 1)
        state.stability = max(1.0, S_new)
        state.difficulty = self._update_difficulty(D, ReinforcementSignal.CORRECT)
        state.last_review = now
        new_phase = state.transition(now)
        if new_phase != state.phase:
            state.phase = new_phase

    @staticmethod
    def _update_difficulty(D: float, signal: ReinforcementSignal) -> float:
        delta_map = {
            ReinforcementSignal.STRONG_CONFIRM: -0.5,
            ReinforcementSignal.PASSIVE_USE: -0.2,
            ReinforcementSignal.WEAK_HIT: 0.0,
            ReinforcementSignal.CORRECT: 1.0,
        }
        delta = delta_map[signal]
        D_new = MEAN_REVERT * D_MEAN + (1 - MEAN_REVERT) * (D + delta)
        return max(1.0, min(10.0, D_new))

    def should_filter(self, R: float) -> bool:
        return R < FORGET_THRESHOLD

    def should_archive(self, R: float) -> bool:
        return R < DREAM_THRESHOLD

    def score(self, similarity: float, state: MemoryState,
              now: float | None = None) -> float:
        R = state.retrievability(now)
        return similarity * R
```

- [ ] **Step 2: 创建 test_fsrs_model.py 单元测试**

```python
# tests/test_fsrs_model.py
"""FSRS-DSR 记忆模型单元测试"""
import math
import time

import pytest

from memory.fsrs_model import (
    FSRSModel, MemoryPhase, MemoryState, ReinforcementSignal,
    estimate_initial_difficulty,
    S_PERMANENT, R_ARCHIVE, R_FORGET, BUFFER_DAYS, S_INIT, D_INIT,
    FORGET_THRESHOLD, DREAM_THRESHOLD,
)


class TestRetrievability:
    def test_buffer_phase_R_is_1(self):
        state = MemoryState(phase=MemoryPhase.BUFFER, created_at=time.time(), last_review=time.time())
        assert state.retrievability() == 1.0

    def test_permanent_phase_R_is_1(self):
        state = MemoryState(phase=MemoryPhase.PERMANENT, stability=50.0,
                            created_at=time.time() - 365*86400, last_review=time.time() - 365*86400)
        assert state.retrievability() == 1.0

    def test_archived_phase_R_is_0(self):
        state = MemoryState(phase=MemoryPhase.ARCHIVED)
        assert state.retrievability() == 0.0

    def test_decay_R_formula(self):
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.DECAY, stability=3.0,
            created_at=now - 3*86400, last_review=now - 3*86400,
        )
        R = state.retrievability(now)
        expected = math.exp(-3.0 / 3.0)
        assert R == pytest.approx(expected, rel=1e-6)

    def test_higher_stability_slower_decay(self):
        now = time.time()
        state_low = MemoryState(phase=MemoryPhase.DECAY, stability=3.0,
                                created_at=now - 30*86400, last_review=now - 30*86400)
        state_high = MemoryState(phase=MemoryPhase.DECAY, stability=30.0,
                                 created_at=now - 30*86400, last_review=now - 30*86400)
        assert state_high.retrievability(now) > state_low.retrievability(now)


class TestTransition:
    def test_buffer_stays_buffer_within_21_days(self):
        state = MemoryState(phase=MemoryPhase.BUFFER, created_at=time.time(), last_review=time.time())
        assert state.transition() == MemoryPhase.BUFFER

    def test_buffer_to_decay_when_no_reinforcement(self):
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT,
            created_at=now - 22*86400, last_review=now - 22*86400,
            reinforcement_count=0,
        )
        assert state.transition(now) == MemoryPhase.DECAY

    def test_buffer_to_reinforced_when_reinforced_but_low_S(self):
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=10.0,
            created_at=now - 22*86400, last_review=now - 22*86400,
            reinforcement_count=1,
        )
        assert state.transition(now) == MemoryPhase.REINFORCED

    def test_buffer_to_permanent_when_S_high(self):
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_PERMANENT,
            created_at=now - 22*86400, last_review=now - 22*86400,
            reinforcement_count=3,
        )
        assert state.transition(now) == MemoryPhase.PERMANENT

    def test_decay_to_archived_when_R_low(self):
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.DECAY, stability=1.0,
            created_at=now - 100*86400, last_review=now - 100*86400,
        )
        assert state.transition(now) == MemoryPhase.ARCHIVED

    def test_reinforced_to_permanent_when_S_high(self):
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.REINFORCED, stability=S_PERMANENT,
            created_at=now - 50*86400, last_review=now - 50*86400,
        )
        assert state.transition(now) == MemoryPhase.PERMANENT


class TestReinforce:
    def test_strong_confirm_increases_S(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT,
            created_at=now, last_review=now,
        )
        old_S = state.stability
        model.reinforce(state, ReinforcementSignal.STRONG_CONFIRM, now)
        assert state.stability > old_S

    def test_correct_decreases_S(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.REINFORCED, stability=10.0,
            created_at=now - 30*86400, last_review=now - 30*86400,
        )
        old_S = state.stability
        model.reinforce(state, ReinforcementSignal.CORRECT, now)
        assert state.stability < old_S

    def test_reinforce_updates_last_review(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT,
            created_at=now - 100, last_review=now - 100,
        )
        model.reinforce(state, ReinforcementSignal.PASSIVE_USE, now)
        assert state.last_review == now

    def test_reinforce_increments_reinforcement_count(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT,
            created_at=now, last_review=now,
        )
        model.reinforce(state, ReinforcementSignal.PASSIVE_USE, now)
        assert state.reinforcement_count == 1

    def test_low_R_gives_bigger_S_boost(self):
        model = FSRSModel()
        now = time.time()
        state_fresh = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT,
            created_at=now, last_review=now,
        )
        state_old = MemoryState(
            phase=MemoryPhase.REINFORCED, stability=S_INIT,
            created_at=now - 30*86400, last_review=now - 30*86400,
        )
        old_S_fresh = state_fresh.stability
        old_S_old = state_old.stability
        model.reinforce(state_fresh, ReinforcementSignal.PASSIVE_USE, now)
        model.reinforce(state_old, ReinforcementSignal.PASSIVE_USE, now)
        growth_fresh = state_fresh.stability - old_S_fresh
        growth_old = state_old.stability - old_S_old
        assert growth_old > growth_fresh

    def test_difficulty_decreases_on_confirm(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT, difficulty=7.0,
            created_at=now, last_review=now,
        )
        model.reinforce(state, ReinforcementSignal.STRONG_CONFIRM, now)
        assert state.difficulty < 7.0

    def test_difficulty_increases_on_correct(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.REINFORCED, stability=10.0, difficulty=5.0,
            created_at=now - 30*86400, last_review=now - 30*86400,
        )
        model.reinforce(state, ReinforcementSignal.CORRECT, now)
        assert state.difficulty > 5.0


class TestEstimateInitialDifficulty:
    def test_default_is_5(self):
        D = estimate_initial_difficulty("一些普通内容")
        assert D == D_INIT

    def test_emotion_increases_D(self):
        D = estimate_initial_difficulty("一些内容", emotion_label="happy")
        assert D > D_INIT

    def test_fact_decreases_D(self):
        D = estimate_initial_difficulty("我的生日是5号")
        assert D < D_INIT

    def test_abstract_increases_D(self):
        D = estimate_initial_difficulty("因为所以意味着本质上原理")
        assert D > D_INIT

    def test_D_clamped_to_1_10(self):
        D_low = estimate_initial_difficulty("生日电话地址名字日期号码")
        D_high = estimate_initial_difficulty("因为所以意味着本质上原理" * 10, emotion_label="angry")
        assert D_low >= 1.0
        assert D_high <= 10.0


class TestFSRSModelScore:
    def test_score_equals_similarity_times_R(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.BUFFER, stability=S_INIT,
            created_at=now, last_review=now,
        )
        score = model.score(0.8, state, now)
        assert score == pytest.approx(0.8, abs=0.01)

    def test_score_decays_for_old_memory(self):
        model = FSRSModel()
        now = time.time()
        state = MemoryState(
            phase=MemoryPhase.DECAY, stability=3.0,
            created_at=now - 100*86400, last_review=now - 100*86400,
        )
        score = model.score(0.8, state, now)
        assert score < 0.05


class TestThresholds:
    def test_should_filter(self):
        model = FSRSModel()
        assert model.should_filter(0.01) is True
        assert model.should_filter(0.5) is False

    def test_should_archive(self):
        model = FSRSModel()
        assert model.should_archive(0.10) is True
        assert model.should_archive(0.5) is False

    def test_threshold_values(self):
        assert FORGET_THRESHOLD == 0.05
        assert DREAM_THRESHOLD == 0.15
        assert S_PERMANENT == 30.0
        assert R_ARCHIVE == 0.05
        assert BUFFER_DAYS == 21
        assert S_INIT == 3.0
        assert D_INIT == 5.0
```

- [ ] **Step 3: 运行测试验证通过**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: Commit**

```bash
git add memory/fsrs_model.py tests/test_fsrs_model.py
git commit -m "feat: add FSRS-DSR memory model (Task 1)"
```

---

### Task 2: 数据库迁移 v15 — 添加 FSRS 列

**Files:**
- Modify: `db/database.py` — 添加 `_migrate_v15` 方法, 更新 CURRENT_SCHEMA_VERSION 和 migrations 列表
- Modify: `db/db_concept.py` — insert_node 增加 difficulty/stability/phase/reinforcement_count/last_review 参数
- Modify: `db/db_memory.py` — 新增 update_fsrs_state, get_memories_since 方法

**Interfaces:**
- Consumes: `MemoryPhase` from `memory.fsrs_model`
- Produces: `DatabaseManager._migrate_v15()`, `MemoryDB.update_fsrs_state()`, `MemoryDB.get_memories_since()`, `ConceptDB.insert_node()` 新参数

- [ ] **Step 1: 写 v15 迁移失败的测试**

```python
# 在 tests/test_fsrs_model.py 末尾追加

class TestDatabaseMigrationV15:
    @pytest.mark.asyncio
    async def test_episodic_memories_has_fsrs_columns(self):
        import aiosqlite
        from db.database import DatabaseManager
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = DatabaseManager(db_path)
            await db.initialize()
            cols = [r["name"] for r in await db.fetch_all("PRAGMA table_info(episodic_memories)")]
            for col in ("difficulty", "stability", "phase", "last_review", "reinforcement_count"):
                assert col in cols, f"episodic_memories missing column: {col}"
            await db.close()

    @pytest.mark.asyncio
    async def test_concept_nodes_has_fsrs_columns(self):
        import aiosqlite
        from db.database import DatabaseManager
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = DatabaseManager(db_path)
            await db.initialize()
            cols = [r["name"] for r in await db.fetch_all("PRAGMA table_info(concept_nodes)")]
            for col in ("difficulty", "stability", "phase", "reinforcement_count", "last_review"):
                assert col in cols, f"concept_nodes missing column: {col}"
            await db.close()

    @pytest.mark.asyncio
    async def test_old_data_default_values(self):
        import aiosqlite
        from db.database import DatabaseManager
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = DatabaseManager(db_path)
            await db.initialize()
            await db._conn.execute(
                "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
                (1000000.0, "test memory"),
            )
            await db._conn.commit()
            row = await db._conn.execute_fetchall(
                "SELECT difficulty, stability, phase, reinforcement_count FROM episodic_memories WHERE summary = 'test memory'"
            )
            assert row[0][0] == 5.0   # difficulty
            assert row[0][1] == 3.0   # stability
            assert row[0][2] == "buffer"  # phase
            assert row[0][3] == 0     # reinforcement_count
            await db.close()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py::TestDatabaseMigrationV15 -v`
Expected: FAIL — 列不存在

- [ ] **Step 3: 在 database.py 中实现 _migrate_v15**

在 `db/database.py` 中:
1. 将 `CURRENT_SCHEMA_VERSION = 14` 改为 `CURRENT_SCHEMA_VERSION = 15`
2. 在 migrations 列表末尾添加: `(15, "episodic_memories+concept_nodes fsrs_dsr_columns", self._migrate_v15),`
3. 添加 `_migrate_v15` 方法:

```python
async def _migrate_v15(self) -> None:
    """v15: FSRS-DSR 记忆模型 — episodic_memories +5 列 + concept_nodes +5 列 + 索引。

    新增列:
    - episodic_memories: difficulty, stability, phase, last_review, reinforcement_count
    - concept_nodes: difficulty, stability, phase, reinforcement_count, last_review

    旧数据迁移:
    - 默认 D=5.0, S=3.0, phase='buffer'
    - access_count >= 5 的标记为 phase='permanent'
    """
    # 1. episodic_memories 新增 5 列（幂等守卫）
    cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
    if "difficulty" not in cols:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN difficulty REAL NOT NULL DEFAULT 5.0"
        )
    if "stability" not in cols:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN stability REAL NOT NULL DEFAULT 3.0"
        )
    if "phase" not in cols:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN phase TEXT NOT NULL DEFAULT 'buffer'"
        )
    if "last_review" not in cols:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN last_review REAL NOT NULL DEFAULT 0"
        )
    if "reinforcement_count" not in cols:
        await self._conn.execute(
            "ALTER TABLE episodic_memories ADD COLUMN reinforcement_count INTEGER NOT NULL DEFAULT 0"
        )

    # 2. concept_nodes 新增 5 列（幂等守卫）
    cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(concept_nodes)")]
    if "difficulty" not in cols:
        await self._conn.execute(
            "ALTER TABLE concept_nodes ADD COLUMN difficulty REAL NOT NULL DEFAULT 5.0"
        )
    if "stability" not in cols:
        await self._conn.execute(
            "ALTER TABLE concept_nodes ADD COLUMN stability REAL NOT NULL DEFAULT 3.0"
        )
    if "phase" not in cols:
        await self._conn.execute(
            "ALTER TABLE concept_nodes ADD COLUMN phase TEXT NOT NULL DEFAULT 'buffer'"
        )
    if "reinforcement_count" not in cols:
        await self._conn.execute(
            "ALTER TABLE concept_nodes ADD COLUMN reinforcement_count INTEGER NOT NULL DEFAULT 0"
        )
    if "last_review" not in cols:
        await self._conn.execute(
            "ALTER TABLE concept_nodes ADD COLUMN last_review TEXT"
        )

    # 3. 旧数据迁移: access_count >= 5 → phase='permanent'
    await self._conn.execute(
        "UPDATE episodic_memories SET phase = 'permanent' WHERE access_count >= 5 AND phase = 'buffer'"
    )
    await self._conn.execute(
        "UPDATE concept_nodes SET phase = 'permanent' WHERE access_count >= 5 AND phase = 'buffer'"
    )

    # 4. 索引
    await self._conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_mem_phase ON episodic_memories(phase);
        CREATE INDEX IF NOT EXISTS idx_mem_stability ON episodic_memories(stability);
        CREATE INDEX IF NOT EXISTS idx_concept_phase ON concept_nodes(phase);
        CREATE INDEX IF NOT EXISTS idx_concept_stability ON concept_nodes(stability);
    """)
```

- [ ] **Step 4: 在 db_memory.py 中添加 update_fsrs_state 和 get_memories_since 方法**

在 `db/db_memory.py` 的 `MemoryDB` 类末尾添加:

```python
async def update_fsrs_state(self, memory_id: int, difficulty: float,
                             stability: float, phase: str,
                             last_review: float,
                             reinforcement_count: int,
                             auto_commit: bool = True) -> None:
    """更新记忆的 FSRS-DSR 状态"""
    await self._conn.execute(
        """UPDATE episodic_memories
           SET difficulty=?, stability=?, phase=?, last_review=?, reinforcement_count=?
           WHERE id=?""",
        (difficulty, stability, phase, last_review, reinforcement_count, memory_id),
    )
    if auto_commit:
        await self._conn.commit()

async def get_memories_since(self, since_ts: float,
                              limit: int = 200) -> list[dict]:
    """获取指定时间戳之后的活跃记忆（用于 7 天回看窗口）"""
    cursor = await self._conn.execute(
        """SELECT * FROM episodic_memories
           WHERE timestamp >= ? AND session_id != 'archived'
           ORDER BY timestamp DESC LIMIT ?""",
        (since_ts, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: 在 db_concept.py 中扩展 insert_node 签名**

在 `db/db_concept.py` 的 `ConceptDB.insert_node` 方法中，在 `source_mem_id` 参数后添加:

```python
async def insert_node(self, id: str, text: str, keys: str,
                      weight: float = 1.0, peak_weight: float = 1.0,
                      confidence: float = 1.0, access_count: int = 0,
                      layer: str = "hippocampus",
                      created: str | None = None,
                      last_accessed: str | None = None,
                      valid_from: str | None = None,
                      valid_to: str | None = None,
                      superseded_by: str | None = None,
                      history: str = "[]",
                      origin: str = "{}",
                      source_mem_id: int | None = None,
                      embedding=None,
                      difficulty: float = 5.0,
                      stability: float = 3.0,
                      phase: str = "buffer",
                      reinforcement_count: int = 0,
                      last_review: str | None = None) -> None:
    """插入概念节点。keys 为 JSON 字符串。"""
    now = created or _now_iso()
    await self._conn.execute(
        """INSERT OR REPLACE INTO concept_nodes
           (id, text, weight, peak_weight, confidence, access_count, keys,
            layer, created, last_accessed, valid_from, valid_to,
            superseded_by, history, origin, source_mem_id, embedding,
            difficulty, stability, phase, reinforcement_count, last_review)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, text, weight, peak_weight, confidence, access_count, keys,
         layer, now, last_accessed or now, valid_from or now, valid_to,
         superseded_by, history, origin, source_mem_id, embedding,
         difficulty, stability, phase, reinforcement_count, last_review),
    )
    await self._conn.commit()
```

- [ ] **Step 6: 运行测试验证通过**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py -v`
Expected: 所有测试 PASS

- [ ] **Step 7: Commit**

```bash
git add db/database.py db/db_memory.py db/db_concept.py tests/test_fsrs_model.py
git commit -m "feat: add DB migration v15 for FSRS-DSR columns (Task 2)"
```

---

### Task 3: 修改 memory_manager.py 评分管线

**Files:**
- Modify: `memory/memory_manager.py` — 替换 FluidMemory 导入, 重写 _apply_fluid_scoring → _apply_fsrs_scoring, 删除 _compute_recency_boost, 更新 _compute_final_scores

**Interfaces:**
- Consumes: `FSRSModel`, `MemoryState`, `MemoryPhase` from `memory.fsrs_model`
- Produces: `MemoryManager._apply_fsrs_scoring()`, 更新的 `_compute_final_scores()`

- [ ] **Step 1: 替换导入**

在 `memory/memory_manager.py` 顶部，将:
```python
from .fluid_memory import FluidMemory
```
替换为:
```python
from .fsrs_model import FSRSModel, MemoryState, MemoryPhase, ReinforcementSignal, estimate_initial_difficulty
```

- [ ] **Step 2: 重写 _apply_fluid_scoring → _apply_fsrs_scoring**

将 `_apply_fluid_scoring` 方法替换为:

```python
async def _apply_fsrs_scoring(self, results: list[dict]) -> list[dict]:
    """FSRS-DSR 记忆评分，过滤低 R 值记忆。

    检索和重排保持只读。访问次数只能在答案明确引用该记忆，或用户明确确认
    记忆有帮助时由消费确认流程更新，避免曝光自反馈偏置。
    """
    if not results:
        return results
    _fsrs = FSRSModel()
    now = time.time()
    filtered: list[dict] = []
    for r in results:
        created_at = r.get("timestamp", time.time())
        access_count = r.get("access_count", 0)
        difficulty = r.get("difficulty", D_INIT)
        stability = r.get("stability", S_INIT)
        phase_str = r.get("phase", "buffer")
        last_review = r.get("last_review", 0) or created_at
        reinforcement_count = r.get("reinforcement_count", 0)

        try:
            phase = MemoryPhase(phase_str)
        except ValueError:
            phase = MemoryPhase.BUFFER

        state = MemoryState(
            difficulty=difficulty,
            stability=stability,
            phase=phase,
            last_review=last_review,
            created_at=created_at,
            reinforcement_count=reinforcement_count,
        )
        R = state.retrievability(now)

        if _fsrs.should_filter(R):
            continue

        r["retrievability"] = R
        r["fluid_score"] = R  # 兼容: fluid_score 字段保留用于调试
        importance = r.get("importance", 0.5)
        r["effective_score"] = importance * R
        filtered.append(r)
    return filtered
```

- [ ] **Step 3: 删除 _compute_recency_boost 方法**

删除整个 `_compute_recency_boost` 方法（约 25 行）。

- [ ] **Step 4: 更新 _compute_final_scores**

将 `_compute_final_scores` 中的评分公式从:
```python
r["final_score"] = (
    rerank_score * 0.5
    + fluid_score * 0.3
    + kg_boost * 0.1
    + recency_boost * 0.1
)
```
替换为:
```python
importance = _normalize_score(r.get("importance", 0.5), default=0.5)
retrievability = _normalize_score(r.get("retrievability"), default=0.5)
r["final_score"] = (
    rerank_score * 0.5
    + retrievability * 0.3
    + kg_boost * 0.1
    + importance * 0.1
)
```

同时删除 `recency_boost` 相关代码行:
```python
# 删除: recency_boost = self._compute_recency_boost(r)
# 删除: r["recency_boost"] = recency_boost
```

将 `r["fluid_score"] = fluid_score` 保留（兼容调试），但 fluid_score 的值现在来自 `_apply_fsrs_scoring` 中设置的 R 值。

- [ ] **Step 5: 更新所有调用点**

将所有 `_apply_fluid_scoring` 调用替换为 `_apply_fsrs_scoring`:
- 约 4 处: `self._apply_fluid_scoring(results)` → `self._apply_fsrs_scoring(results)`

- [ ] **Step 6: 运行现有测试验证**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_strict_memory_reinforcement.py -v`
Expected: PASS（测试只检查 _apply_fluid_scoring 不写 access_count，新方法同样只读）

- [ ] **Step 7: Commit**

```bash
git add memory/memory_manager.py
git commit -m "feat: replace fluid scoring with FSRS-DSR scoring pipeline (Task 3)"
```

---

### Task 4: 修改 confirm_correct.py — 用 S/D 更新替代 weight+0.15

**Files:**
- Modify: `memory/confirm_correct.py`

**Interfaces:**
- Consumes: `FSRSModel`, `MemoryState`, `ReinforcementSignal` from `memory.fsrs_model`

- [ ] **Step 1: 添加 FSRS 导入**

在 `memory/confirm_correct.py` 顶部添加:
```python
import time as _time

from memory.fsrs_model import (
    FSRSModel, MemoryState, MemoryPhase, ReinforcementSignal,
    estimate_initial_difficulty,
)
```

- [ ] **Step 2: 重写 confirm 方法**

将 `confirm` 方法替换为:

```python
async def confirm(self, node_ids: list[str]) -> dict:
    """确认强化 — FSRS-DSR 模型

    1. 从节点读取 FSRS 状态 (difficulty, stability, phase, last_review, reinforcement_count)
    2. 构建 MemoryState, 调用 FSRSModel.reinforce(STRONG_CONFIRM)
    3. 写回更新后的 FSRS 状态 + access_count + weight + peak_weight + last_accessed
    4. 强化所有关联边 weight += 0.25 (双向同步)
    5. 同步 episodic_memories.access_count + FSRS 状态
    """
    _fsrs = FSRSModel()
    now = _time.time()
    now_iso = self._now_iso()
    reinforced = 0
    unknown = 0

    for nid in node_ids:
        node = await self.db.get_node(nid)
        if node is None:
            unknown += 1
            continue

        new_access = node["access_count"] + 1
        new_weight = min(1.0, node["weight"] + self.BOOST_PER_ACCESS)
        new_peak = max(node["peak_weight"], new_weight)

        difficulty = node.get("difficulty", 5.0)
        stability = node.get("stability", 3.0)
        phase_str = node.get("phase", "buffer")
        last_review = node.get("last_review")
        reinforcement_count = node.get("reinforcement_count", 0)
        created_at_str = node.get("created", now_iso)

        try:
            phase = MemoryPhase(phase_str)
        except ValueError:
            phase = MemoryPhase.BUFFER

        try:
            created_at = _parse_iso_to_ts(created_at_str)
        except Exception:
            created_at = now

        lr = _parse_iso_to_ts(last_review) if last_review else created_at

        state = MemoryState(
            difficulty=difficulty,
            stability=stability,
            phase=phase,
            last_review=lr,
            created_at=created_at,
            reinforcement_count=reinforcement_count,
        )

        _fsrs.reinforce(state, ReinforcementSignal.STRONG_CONFIRM, now)

        await self.db.update_node(
            nid,
            access_count=new_access,
            weight=new_weight,
            peak_weight=new_peak,
            last_accessed=now_iso,
            difficulty=state.difficulty,
            stability=state.stability,
            phase=state.phase.value,
            last_review=now_iso,
            reinforcement_count=state.reinforcement_count,
        )

        edges = await self.db.get_edges(nid)
        for target_id, edge in edges.items():
            new_edge_w = min(1.0, edge["weight"] + self.EDGE_BOOST)
            await self.db.update_edge(nid, target_id, weight=new_edge_w)
            await self.db.update_edge(target_id, nid, weight=new_edge_w)

        if node.get("source_mem_id"):
            try:
                await self.memory.increment_access_count(node["source_mem_id"])
                await self.memory.update_fsrs_state(
                    node["source_mem_id"],
                    difficulty=state.difficulty,
                    stability=state.stability,
                    phase=state.phase.value,
                    last_review=now,
                    reinforcement_count=state.reinforcement_count,
                )
            except Exception as e:
                logger.debug("confirm.sync_episodic_failed", error=str(e))

        reinforced += 1

    return {"reinforced": reinforced, "unknown": unknown}
```

- [ ] **Step 3: 添加 _parse_iso_to_ts 辅助函数**

在 `confirm_correct.py` 模块级别添加:

```python
def _parse_iso_to_ts(iso_str: str | None) -> float:
    """将 ISO 格式时间字符串转换为 Unix 时间戳"""
    if not iso_str:
        return 0.0
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except Exception:
        return 0.0
```

- [ ] **Step 4: 修改 correct 方法 — 新节点继承 FSRS 状态**

在 `correct` 方法中，创建新节点时添加 FSRS 参数:

将 `insert_node` 调用从:
```python
await self.db.insert_node(
    id=new_id, text=self._clean_text(new_text),
    weight=old_node.get("weight", 1.0),
    peak_weight=old_node.get("peak_weight", 1.0),
    confidence=lowered_conf, access_count=0,
    keys=json.dumps(new_keys, ensure_ascii=False),
    layer="hippocampus",
    created=now, last_accessed=now,
    valid_from=now, valid_to=None,
    superseded_by=None, history=json.dumps(history, ensure_ascii=False),
    origin=json.dumps({"via": "correct"}),
)
```
改为:
```python
await self.db.insert_node(
    id=new_id, text=self._clean_text(new_text),
    weight=old_node.get("weight", 1.0),
    peak_weight=old_node.get("peak_weight", 1.0),
    confidence=lowered_conf, access_count=0,
    keys=json.dumps(new_keys, ensure_ascii=False),
    layer="hippocampus",
    created=now, last_accessed=now,
    valid_from=now, valid_to=None,
    superseded_by=None, history=json.dumps(history, ensure_ascii=False),
    origin=json.dumps({"via": "correct"}),
    difficulty=old_node.get("difficulty", 5.0),
    stability=max(1.0, old_node.get("stability", 3.0) * 0.5),
    phase="buffer",
    reinforcement_count=0,
    last_review=now,
)
```

- [ ] **Step 5: 运行测试**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_confirm_correct.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add memory/confirm_correct.py
git commit -m "feat: confirm/correct use FSRS-DSR S/D updates (Task 4)"
```

---

### Task 5: 修改 concept_graph.py — 传入 D 初始化参数

**Files:**
- Modify: `memory/concept_graph.py`

**Interfaces:**
- Consumes: `estimate_initial_difficulty` from `memory.fsrs_model`

- [ ] **Step 1: 添加 FSRS 导入**

在 `memory/concept_graph.py` 顶部添加:
```python
from memory.fsrs_model import estimate_initial_difficulty
```

- [ ] **Step 2: 修改 remember 方法 — 传入 difficulty 和 phase**

将 `remember` 方法中的 `insert_node` 调用从:
```python
await self.db.insert_node(
    id=node_id, text=cleaned,
    keys=json.dumps(keys, ensure_ascii=False),
    weight=1.0, peak_weight=1.0, confidence=1.0,
    access_count=0, layer="hippocampus",
    created=now, last_accessed=now,
    valid_from=now, valid_to=None,
    source_mem_id=source_mem_id,
)
```
改为:
```python
difficulty = estimate_initial_difficulty(cleaned)
await self.db.insert_node(
    id=node_id, text=cleaned,
    keys=json.dumps(keys, ensure_ascii=False),
    weight=1.0, peak_weight=1.0, confidence=1.0,
    access_count=0, layer="hippocampus",
    created=now, last_accessed=now,
    valid_from=now, valid_to=None,
    source_mem_id=source_mem_id,
    difficulty=difficulty,
    stability=3.0,
    phase="buffer",
    reinforcement_count=0,
    last_review=now,
)
```

- [ ] **Step 3: 运行测试**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_concept_graph.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add memory/concept_graph.py
git commit -m "feat: concept_graph uses FSRS difficulty initialization (Task 5)"
```

---

### Task 6: 修改 dream_consolidation.py — 用 FSRSModel 替代 FluidMemory

**Files:**
- Modify: `core/dream_consolidation.py`

**Interfaces:**
- Consumes: `FSRSModel`, `MemoryState`, `MemoryPhase` from `memory.fsrs_model`

- [ ] **Step 1: 替换导入**

将:
```python
from memory.fluid_memory import FluidMemory
```
替换为:
```python
from memory.fsrs_model import FSRSModel, MemoryState, MemoryPhase
```

- [ ] **Step 2: 替换 DreamConsolidator 中的 FluidMemory 使用**

将 `__init__` 中的:
```python
self._fluid_scorer = FluidMemory()
```
替换为:
```python
self._fsrs = FSRSModel()
```

- [ ] **Step 3: 重写 consolidate 方法中的衰减评分**

将 `consolidate` 方法中的:
```python
fm_score = self._fluid_scorer.score(
    similarity=m.importance,
    created_at=m.created_at,
    access_count=m.access_count,
)
m.strength = fm_score
```
替换为:
```python
state = MemoryState(
    stability=m.strength if m.strength > 0 else 3.0,
    created_at=m.created_at,
    last_review=m.last_access,
    phase=MemoryPhase.BUFFER if m.access_count >= 5 else MemoryPhase.DECAY,
)
R = state.retrievability(now)
m.strength = R
```

- [ ] **Step 4: 重写 consolidate_db 方法**

将 `consolidate_db` 方法替换为:

```python
async def consolidate_db(self, memory_db: Any, batch_size: int = 100) -> int:
    """数据库归档 — 遍历活跃记忆, 低 R 值归档 (FSRS-DSR 模型)"""
    archived_count = 0
    try:
        memories = await memory_db.get_all_memories(limit=batch_size)
        to_archive: list = []
        now = time.time()
        for mem in memories:
            mem_id = mem.get("id")
            created_at = mem.get("timestamp", time.time())
            access_count = mem.get("access_count", 0)
            difficulty = mem.get("difficulty", 5.0)
            stability = mem.get("stability", 3.0)
            phase_str = mem.get("phase", "buffer")
            last_review = mem.get("last_review", 0) or created_at
            reinforcement_count = mem.get("reinforcement_count", 0)

            try:
                phase = MemoryPhase(phase_str)
            except ValueError:
                phase = MemoryPhase.BUFFER

            state = MemoryState(
                difficulty=difficulty,
                stability=stability,
                phase=phase,
                last_review=last_review,
                created_at=created_at,
                reinforcement_count=reinforcement_count,
            )
            R = state.retrievability(now)
            importance = mem.get("importance", 0.5)
            if self._fsrs.should_archive(R) and importance < self._importance_threshold:
                to_archive.append(mem_id)
        if to_archive:
            await memory_db.archive_memories_batch(to_archive)
            archived_count = len(to_archive)
        logger.info(f"Dream.consolidate_db archived={archived_count}")
    except Exception as e:
        logger.error(f"Dream.consolidate_db_failed: {e}")
    return archived_count
```

- [ ] **Step 5: 重写 consolidate_from_db 中的衰减评分**

在 `consolidate_from_db` 方法中，将:
```python
fm_score = self._fluid_scorer.score(
    similarity=m.importance,
    created_at=m.created_at,
    access_count=m.access_count,
)
m.strength = fm_score
```
替换为:
```python
state = MemoryState(
    difficulty=mem.get("difficulty", 5.0),
    stability=mem.get("stability", 3.0),
    phase=_parse_phase(mem.get("phase", "buffer")),
    last_review=mem.get("last_review", 0) or mem.get("timestamp", time.time()),
    created_at=mem.get("timestamp", time.time()),
    reinforcement_count=mem.get("reinforcement_count", 0),
)
R = state.retrievability(now)
m.strength = R
```

在 `consolidate_from_db` 方法中，将归档判断从:
```python
if (self._fluid_scorer.should_archive(fm_score)
        and m.importance < self._importance_threshold):
```
替换为:
```python
if (self._fsrs.should_archive(R)
        and m.importance < self._importance_threshold):
```

在模块级别添加辅助函数:
```python
def _parse_phase(phase_str: str) -> MemoryPhase:
    try:
        return MemoryPhase(phase_str)
    except ValueError:
        return MemoryPhase.BUFFER
```

- [ ] **Step 6: 运行测试**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_dream_non_destructive.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add core/dream_consolidation.py
git commit -m "feat: dream_consolidation uses FSRS-DSR model (Task 6)"
```

---

### Task 7: 更新 fluid_memory.py — 添加兼容层

**Files:**
- Modify: `memory/fluid_memory.py`

**Interfaces:**
- Produces: `FluidMemory` 类保持向后兼容，内部委托给 FSRSModel

- [ ] **Step 1: 重写 fluid_memory.py 为兼容层**

```python
"""流体记忆系统 — 兼容层 (已迁移到 fsrs_model.py)

此模块保留用于向后兼容。新代码应直接使用 memory.fsrs_model。
"""
import math
import time

from loguru import logger

from memory.fsrs_model import (
    FSRSModel, MemoryState, MemoryPhase, ReinforcementSignal,
    S_INIT, BUFFER_DAYS, FORGET_THRESHOLD, DREAM_THRESHOLD,
)


class FluidMemory:
    """流体记忆 — 兼容层

    旧接口 score(similarity, created_at, access_count, peak_weight) 保持可用，
    内部委托给 FSRSModel。peak_weight 参数已废弃（忽略）。
    """

    STABILITY_BASE_DAYS = S_INIT
    STABILITY_PER_ACCESS = 14.0
    BOOST_PER_ACCESS = 0.15
    GRACE_DAYS = BUFFER_DAYS
    PERMANENT_ACCESS_THRESHOLD = 5
    WEIGHT_THRESHOLD = 0.1
    FORGET_THRESHOLD = FORGET_THRESHOLD
    DREAM_THRESHOLD = DREAM_THRESHOLD

    def __init__(self) -> None:
        self._fsrs = FSRSModel()

    def score(self, similarity: float, created_at: float,
              access_count: int = 0, peak_weight: float = 1.0) -> float:
        """兼容旧接口 — 内部使用 FSRS-DSR 模型"""
        now = time.time()
        days = max(0, (now - created_at) / 86400.0)

        if access_count >= self.PERMANENT_ACCESS_THRESHOLD:
            return similarity
        if days <= self.GRACE_DAYS:
            return similarity

        stability = self.STABILITY_BASE_DAYS + access_count * self.STABILITY_PER_ACCESS
        retention = math.exp(-days / stability)
        return similarity * retention

    def is_permanent(self, access_count: int) -> bool:
        return access_count >= self.PERMANENT_ACCESS_THRESHOLD

    def should_filter(self, score: float) -> bool:
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        return score < self.DREAM_THRESHOLD
```

- [ ] **Step 2: 运行旧测试验证兼容性**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fluid_memory.py -v`
Expected: PASS

- [ ] **Step 3: 运行 audit 测试验证兼容性**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_audit_batch_fixes.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add memory/fluid_memory.py
git commit -m "feat: fluid_memory compat layer delegates to FSRSModel (Task 7)"
```

---

### Task 8: 全量集成测试

**Files:**
- Test: `tests/test_fsrs_model.py`, `tests/test_fluid_memory.py`, `tests/test_confirm_correct.py`, `tests/test_dream_non_destructive.py`, `tests/test_strict_memory_reinforcement.py`, `tests/test_audit_batch_fixes.py`

- [ ] **Step 1: 运行全量测试**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py tests/test_fluid_memory.py tests/test_confirm_correct.py tests/test_dream_non_destructive.py tests/test_strict_memory_reinforcement.py tests/test_audit_batch_fixes.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 运行 lint 检查**

Run: `cd f:\naxida\xiaoda-agent && python -m ruff check memory/fsrs_model.py memory/fluid_memory.py memory/memory_manager.py memory/confirm_correct.py memory/concept_graph.py core/dream_consolidation.py db/database.py db/db_memory.py db/db_concept.py`
Expected: 无错误

- [ ] **Step 3: 修复任何 lint 或测试问题**

如有问题，修复后重新运行。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: FSRS-DSR memory system complete (Task 8)"
```