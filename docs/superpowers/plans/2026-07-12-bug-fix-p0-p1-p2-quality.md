# Bug Fix Implementation Plan (P0→P1→P2→Quality)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 73 bugs across P0/P1/P2 and improve quality score from 7.04→7.84

**Architecture:** Fix in dependency order — P0 first (fatal), then P1 (important), then P2 (minor), then quality improvements. Within each priority, fix bottom-up (infrastructure → memory → channel → J-Space).

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, loguru, pytest

## Global Constraints

- All fixes must pass existing test suite (`pytest tests/ -v`)
- No breaking changes to public APIs unless spec explicitly requires it
- Database migrations must be idempotent (safe to re-run)
- Follow existing code style: loguru logger, no type comments, `from __future__ import annotations`
- Commit message format: `fix: description (P0-XX)` or `fix: description (P1-XX)` etc.

---

## Phase 1: P0 — Fatal Bugs (13 bugs)

### Task 1: P0-09 — background_tasks._spawn() 无事件循环保护

**Files:**
- Modify: `core/background_tasks.py:63`

**Interfaces:**
- Consumes: `asyncio.create_task`, `asyncio.get_running_loop`
- Produces: `_spawn()` that gracefully handles missing event loop

- [ ] **Step 1: Write the failing test**

```python
# tests/test_background_tasks_spawn.py
import asyncio
from core.background_tasks import _spawn, _bg_tasks


def test_spawn_no_running_loop():
    """_spawn() outside async context should not crash, just log error."""
    async def dummy():
        pass

    # Calling _spawn outside of any event loop should not raise
    _spawn(dummy())
    # No task created since there's no running loop
    assert len(_bg_tasks) == 0


@pytest.mark.asyncio
async def test_spawn_with_running_loop():
    """_spawn() inside async context should create task normally."""
    from core.background_tasks import _bg_tasks
    before = len(_bg_tasks)

    async def dummy():
        pass

    _spawn(dummy())
    assert len(_bg_tasks) == before + 1

    # Cleanup
    for t in list(_bg_tasks):
        if not t.done():
            t.cancel()
    _bg_tasks.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_background_tasks_spawn.py -v`
Expected: `test_spawn_no_running_loop` FAILS (RuntimeError from create_task)

- [ ] **Step 3: Write minimal implementation**

In `core/background_tasks.py`, replace the `_spawn` function body:

```python
def _spawn(coro: Any) -> None:
    """创建 fire-and-forget 后台任务，自动从 _bg_tasks 中移除已完成的任务。

    包含耗时监控：任务完成时记录执行时长，超过 30s 发出告警日志。
    包含 loop 保护：同步上下文调用时降级日志而非崩溃。
    """
    task_name = getattr(coro, '__name__', coro.__class__.__name__)
    start_time = time.time()

    async def _wrapped():
        try:
            await coro
        finally:
            elapsed = time.time() - start_time
            if elapsed > 30:
                logger.warning("bg.task_slow name={} elapsed={:.1f}s", task_name, elapsed)
            else:
                logger.debug("bg.task_done name={} elapsed={:.1f}s", task_name, elapsed)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("bg.spawn_no_loop: cannot create task without running event loop, "
                     "task={} will be dropped", task_name)
        return
    task = loop.create_task(_wrapped())
    _bg_tasks.add(task)
    task.add_done_callback(_on_bg_task_done)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_background_tasks_spawn.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/background_tasks.py tests/test_background_tasks_spawn.py
git commit -m "fix: _spawn() graceful degradation without running loop (P0-09)"
```

---

### Task 2: P0-05 — EventBus ContextVar 绑定泄漏

**Files:**
- Modify: `core/event_bus.py:105-110`
- Modify: `qq_bot_adapter.py` (bind_user/unbind_user call sites)
- Modify: `web/ws_hub.py` (bind_user/unbind_user call sites)
- Modify: `cli_client.py` (bind_user/unbind_user call sites)

**Interfaces:**
- Consumes: `contextvars.ContextVar`, `contextvars.Token`
- Produces: `bind_user() -> Token`, `unbind_user(Token) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_bus_token.py
import asyncio
import contextvars
from core.event_bus import AgentEventBus, _current_user


class FakeUser:
    async def deliver(self, event):
        pass


@pytest.mark.asyncio
async def test_bind_unbind_token_isolation():
    """Two concurrent coroutines binding different users should not interfere."""
    bus = AgentEventBus()
    user_a = FakeUser()
    user_b = FakeUser()

    results = {}

    async def coro_a():
        token = bus.bind_user(user_a)
        await asyncio.sleep(0.01)
        results['a'] = bus.bound_user
        bus.unbind_user(token)
        await asyncio.sleep(0.01)
        results['a_after'] = bus.bound_user

    async def coro_b():
        token = bus.bind_user(user_b)
        await asyncio.sleep(0.01)
        results['b'] = bus.bound_user
        bus.unbind_user(token)
        await asyncio.sleep(0.01)
        results['b_after'] = bus.bound_user

    await asyncio.gather(coro_a(), coro_b())
    # Each coroutine should have seen its own user
    assert results['a'] is user_a
    assert results['b'] is user_b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_event_bus_token.py -v`
Expected: FAIL (unbind_user clears global ContextVar, corrupting other coroutine)

- [ ] **Step 3: Modify event_bus.py**

```python
# core/event_bus.py — replace bind_user / unbind_user

import contextvars

class AgentEventBus:
    def bind_user(self, user: "UserBase") -> contextvars.Token:
        """绑定当前 session 的 User。返回 Token，调用方必须在 finally 中调用 unbind_user(token)。"""
        return _current_user.set(user)

    def unbind_user(self, token: contextvars.Token) -> None:
        """解绑 User（session 结束时调用）。必须传入 bind_user 返回的 Token。"""
        try:
            _current_user.reset(token)
        except (ValueError, LookupError):
            logger.debug("event_bus.unbind_noop: token already consumed or context mismatch")
```

- [ ] **Step 4: Update all call sites**

Find all `bind_user` / `unbind_user` call sites and convert to token pattern:

```python
# Pattern: before
# event_bus.bind_user(user)
# ...
# event_bus.unbind_user()

# Pattern: after
token = event_bus.bind_user(user)
try:
    ...
finally:
    event_bus.unbind_user(token)
```

Search with: `grep -rn "bind_user\|unbind_user" --include="*.py" f:\naxida\xiaoda-agent`

Update each call site in: `qq_bot_adapter.py`, `web/ws_hub.py`, `cli_client.py`

- [ ] **Step 5: Run test to verify it passes**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_event_bus_token.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/event_bus.py qq_bot_adapter.py web/ws_hub.py cli_client.py tests/test_event_bus_token.py
git commit -m "fix: EventBus ContextVar token-based bind/unbind prevents cross-coroutine leak (P0-05)"
```

---

### Task 3: P0-07 — StructuredBlackboard tag/direction 索引不过期清理

**Files:**
- Modify: `agent_core/structured_blackboard.py`

**Interfaces:**
- Consumes: `SharedBlackboard.cleanup_expired()`, `SharedBlackboard.keys()`
- Produces: `StructuredBlackboard.cleanup_expired()` override

- [ ] **Step 1: Write the failing test**

```python
# tests/test_structured_blackboard_index.py
import asyncio
import time
import pytest
from agent_core.structured_blackboard import StructuredBlackboard


@pytest.mark.asyncio
async def test_index_cleanup_on_expiry():
    """After TTL expiry and cleanup, tag/direction indexes should not reference stale keys."""
    bb = StructuredBlackboard(default_ttl=0.1)
    await bb.put_structured("k1", "v1", tags=["t1"], direction="d1", ttl=0.1)
    await bb.put_structured("k2", "v2", tags=["t1"], direction="d2", ttl=60.0)

    # Before expiry
    assert "k1" in bb._tag_index.get("t1", set())
    assert "k1" in bb._direction_index.get("d1", set())

    await asyncio.sleep(0.15)
    cleaned = await bb.cleanup_expired()

    # k1 should be removed from indexes
    assert "k1" not in bb._tag_index.get("t1", set())
    assert "d1" not in bb._direction_index
    # k2 should still be there
    assert "k2" in bb._tag_index.get("t1", set())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_structured_blackboard_index.py -v`
Expected: FAIL (k1 still in _tag_index after expiry)

- [ ] **Step 3: Add cleanup_expired override**

```python
# agent_core/structured_blackboard.py — add method to StructuredBlackboard class

    async def cleanup_expired(self) -> int:
        """清理过期条目并同步清理 tag/direction 索引。"""
        cleaned = await super().cleanup_expired()
        if cleaned == 0:
            return 0

        alive_keys = set(await self.keys())

        stale_tags = []
        for tag, keys in self._tag_index.items():
            before = len(keys)
            keys.difference_update(alive_keys)
            if before > 0 and len(keys) == 0:
                stale_tags.append(tag)
        for tag in stale_tags:
            del self._tag_index[tag]

        stale_dirs = []
        for direction, keys in self._direction_index.items():
            before = len(keys)
            keys.difference_update(alive_keys)
            if before > 0 and len(keys) == 0:
                stale_dirs.append(direction)
        for direction in stale_dirs:
            del self._direction_index[direction]

        return cleaned
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_structured_blackboard_index.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_core/structured_blackboard.py tests/test_structured_blackboard_index.py
git commit -m "fix: StructuredBlackboard index cleanup on TTL expiry (P0-07)"
```

---

### Task 4: P0-01 + P0-02 — FSRS 遗忘公式反转 + 极小值截断

**Files:**
- Modify: `memory/fsrs_model.py:152-153` (_apply_forget)
- Modify: `memory/fsrs_model.py:135-146` (_apply_recall — fix double construction)

**Interfaces:**
- Consumes: `S_INIT = 3.0`
- Produces: Correct `_apply_forget` with `S_INIT` not `S * 0.5`, `_compute_phase` helper

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fsrs_forget_fix.py
import math
import time
from memory.fsrs_model import FSRSModel, MemoryState, MemoryPhase, ReinforcementSignal, S_INIT


def test_forget_decreases_stability():
    """After forgetting, S_new must be < S_old (P0-01)."""
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
    """_apply_forget should use S_INIT as base, not current S (P0-01 core fix)."""
    # S=300, D=5.0: S_new = S_INIT * 5^(-0.3) * ((301)^0.2 - 1) ≈ 6.6
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
    """_apply_recall should increment reinforcement_count by exactly 1, not 2 (P1 Bug #8)."""
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_forget_fix.py -v`
Expected: `test_forget_decreases_stability` FAILS (S_new ≈ 470 > 300)

- [ ] **Step 3: Fix _apply_forget and refactor _apply_recall**

Replace in `memory/fsrs_model.py`:

```python
    def _compute_phase(self, D: float, S: float, state: MemoryState,
                       now: float) -> MemoryPhase:
        """根据新的 D/S 和当前状态计算目标 phase，无需构造完整 MemoryState。"""
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
```

- [ ] **Step 4: Run all FSRS tests**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py tests/test_fsrs_forget_fix.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add memory/fsrs_model.py tests/test_fsrs_forget_fix.py
git commit -m "fix: FSRS forget formula uses S_INIT not S*0.5, refactor double construction (P0-01, P0-02, P1-#8)"
```

---

### Task 5: P0-04 + P0-06 + P0-03 — DB v16 migration + encode_memory FSRS init + confirm_correct created_at

**Files:**
- Modify: `db/database.py` (add _migrate_v16)
- Modify: `memory/memory_manager.py:1480,1489,1651` (_apply_fsrs_scoring + encode_memory)
- Modify: `memory/confirm_correct.py:70` (created_at fix)

**Interfaces:**
- Consumes: `S_INIT`, `estimate_initial_difficulty`, `time.time()`
- Produces: v16 migration, encode_memory sets FSRS fields, confirm_correct reads correct created_at

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fsrs_init_fix.py
import time
from memory.fsrs_model import FSRSModel, MemoryState, MemoryPhase, S_INIT


def test_new_memory_not_filtered():
    """New memory with last_review=now should have R≈1.0 and not be filtered (P0-06)."""
    fsrs = FSRSModel()
    now = time.time()
    state = MemoryState(
        difficulty=5.0, stability=S_INIT,
        phase=MemoryPhase.BUFFER,
        last_review=now, created_at=now,
        reinforcement_count=0,
    )
    R = state.retrievability(now)
    assert R > 0.9, f"New memory should have R≈1.0, got {R}"
    assert not fsrs.should_filter(R), "New memory should not be filtered"


def test_old_memory_last_review_zero_handled():
    """Memory with last_review=0 should be handled gracefully (P0-06 defensive)."""
    # When last_review=0, elapsed_days ≈ 20500, R ≈ 0 for any reasonable S
    now = time.time()
    state = MemoryState(
        difficulty=5.0, stability=S_INIT,
        phase=MemoryPhase.BUFFER,
        last_review=0.0, created_at=0.0,
        reinforcement_count=0,
    )
    # BUFFER phase returns R=1.0 regardless, so this is safe
    R = state.retrievability(now)
    assert R == 1.0, f"BUFFER memory should always return R=1.0, got {R}"
```

- [ ] **Step 2: Add v16 migration to database.py**

Find the migration dispatch logic in `db/database.py` and add:

```python
    async def _migrate_v16(self) -> None:
        """v16: Add created_at REAL column + backfill last_review=0 rows."""
        concept_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(concept_nodes)")]
        if "created_at" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN created_at REAL DEFAULT 0"
            )

        epi_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "created_at" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN created_at REAL DEFAULT 0"
            )

        # Backfill concept_nodes.created_at from created ISO string
        await self._conn.execute("""
            UPDATE concept_nodes
            SET created_at = CAST(
                (julianday(substr(created, 1, 19)) - julianday('1970-01-01')) * 86400 AS REAL)
            WHERE created_at = 0 AND created IS NOT NULL AND created != ''
        """)

        # Backfill episodic_memories.created_at from timestamp
        await self._conn.execute("""
            UPDATE episodic_memories
            SET created_at = timestamp
            WHERE created_at = 0 AND timestamp > 0
        """)

        # Backfill last_review=0 rows
        await self._conn.execute("""
            UPDATE episodic_memories
            SET last_review = timestamp
            WHERE last_review = 0 AND timestamp > 0
        """)

        await self._conn.commit()
        logger.info("database.migration_v16_created_at_done")
```

Update `CURRENT_SCHEMA_VERSION` to 16 and add the migration dispatch call.

- [ ] **Step 3: Fix _apply_fsrs_scoring in memory_manager.py**

```python
    async def _apply_fsrs_scoring(self, results: list[dict]) -> list[dict]:
        if not results:
            return results
        if not hasattr(self, '_fsrs'):
            self._fsrs = FSRSModel()
        now = time.time()
        filtered: list[dict] = []
        for r in results:
            similarity = r.get("score", 0.5)
            last_review = r.get("last_review", 0.0)
            created_at = r.get("created_at", 0.0) or r.get("timestamp", 0.0)
            if last_review == 0.0:
                last_review = r.get("timestamp", 0.0)
            try:
                phase = MemoryPhase(r.get("phase", "buffer"))
            except ValueError:
                logger.warning("fsrs_invalid_phase id={} phase={}", r.get("id"), r.get("phase"))
                phase = MemoryPhase.BUFFER
            state = MemoryState(
                difficulty=r.get("difficulty", 5.0),
                stability=r.get("stability", 3.0),
                phase=phase,
                last_review=last_review,
                created_at=created_at,
                reinforcement_count=r.get("reinforcement_count", 0),
            )
            R = state.retrievability(now)
            fsrs_score = self._fsrs.score(similarity, state, now)
            if self._fsrs.should_filter(R):
                continue
            r["fluid_score"] = R
            r["fsrs_score"] = fsrs_score
            importance = r.get("importance", 0.5)
            r["effective_score"] = importance * fsrs_score
            filtered.append(r)
        return filtered
```

- [ ] **Step 4: Fix encode_memory to set FSRS fields**

In `memory_manager.py` `encode_memory` method, after `insert_episodic_memory`, add:

```python
            # Initialize FSRS state for new memory
            now_ts = time.time()
            initial_difficulty = estimate_initial_difficulty(summary, emotion)
            await self.memory.update_episodic_fsrs(
                mem_id,
                difficulty=initial_difficulty,
                stability=S_INIT,
                phase="buffer",
                last_review=now_ts,
                reinforcement_count=0,
            )
```

Note: If `update_episodic_fsrs` doesn't exist, add it to `db/db_memory.py` or use direct SQL.

- [ ] **Step 5: Fix confirm_correct.py created_at**

Replace line 70 in `confirm_correct.py`:

```python
            _created_at = node.get("created_at", 0.0)
            if _created_at == 0.0:
                _created_iso = node.get("created", "")
                if _created_iso:
                    try:
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        dt = datetime.fromisoformat(_created_iso)
                        _created_at = dt.timestamp()
                    except (ValueError, TypeError):
                        _created_at = 0.0
            state = MemoryState(
                difficulty=node.get("difficulty", 5.0),
                stability=node.get("stability", 3.0),
                phase=MemoryPhase(node.get("phase", "buffer")),
                last_review=node.get("last_review", 0.0) or now_ts,
                created_at=_created_at if _created_at > 0.0 else now_ts,
                reinforcement_count=node.get("reinforcement_count", 0),
            )
```

- [ ] **Step 6: Run tests**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/test_fsrs_model.py tests/test_fsrs_init_fix.py tests/test_fsrs_forget_fix.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add db/database.py memory/memory_manager.py memory/confirm_correct.py tests/test_fsrs_init_fix.py
git commit -m "fix: v16 migration, encode_memory FSRS init, confirm_correct created_at (P0-03, P0-04, P0-06)"
```

---

### Task 6: P0-08 — consolidate_from_db difficulty 硬编码

**Files:**
- Modify: `core/dream_consolidation.py:~L280` (consolidate_from_db)

**Interfaces:**
- Consumes: DB row's `difficulty` field
- Produces: MemoryState with correct difficulty from DB

- [ ] **Step 1: Fix consolidate_from_db**

In `core/dream_consolidation.py` `consolidate_from_db` method, find the Decay section where MemoryState is constructed with `difficulty=1.0` and replace:

```python
            # 2. Decay — FSRS-DSR Retrievability 衰减评分
            evict_ids: list[str] = []
            for mid, m in memories.items():
                state = MemoryState(
                    difficulty=m._db_difficulty if hasattr(m, '_db_difficulty') else m.importance * 10.0,
                    stability=m.strength * 10.0 if m.strength > 0 else 3.0,
                    phase=MemoryPhase.REINFORCED,
                    last_review=m.last_access,
                    created_at=m.created_at,
                    reinforcement_count=m.access_count,
                )
```

Also fix the Memory object creation to store DB difficulty:

```python
            for row in rows:
                mid = str(row["id"])
                mem = Memory(
                    id=mid,
                    content=row.get("summary", ""),
                    importance=row.get("importance", 0.5),
                    strength=1.0,
                    last_access=row.get("last_review", 0.0) or row.get("timestamp", time.time()),
                    created_at=row.get("created_at", 0.0) or row.get("timestamp", time.time()),
                    access_count=row.get("access_count", 0),
                )
                mem._db_difficulty = row.get("difficulty", 5.0)
                mem._db_stability = row.get("stability", 3.0)
                mem._db_phase = row.get("phase", "buffer")
                mem._db_reinforcement_count = row.get("reinforcement_count", 0)
                memories[mid] = mem
```

Also fix `m.strength = R` to `m.strength = max(m.strength * 0.95, R)` (P2-MEM-05).

- [ ] **Step 2: Add FSRS state writeback at end of consolidate_from_db**

```python
            # Batch writeback FSRS state to database
            if memory_db and hasattr(memory_db, '_conn'):
                updates = []
                for mid, m in memories.items():
                    if hasattr(m, '_db_difficulty'):
                        updates.append((
                            m._db_difficulty,
                            m._db_stability,
                            m._db_phase,
                            m.last_access,
                            int(mid),
                        ))
                if updates:
                    try:
                        await memory_db._conn.executemany(
                            """UPDATE episodic_memories
                               SET difficulty=?, stability=?, phase=?, last_review=?
                               WHERE id=?""",
                            updates
                        )
                        await memory_db._conn.commit()
                    except Exception as e:
                        logger.debug(f"Dream.fsrs_writeback_failed: {e}")
```

- [ ] **Step 3: Run tests**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/ -v -k "dream or fsrs" --tb=short`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add core/dream_consolidation.py
git commit -m "fix: consolidate_from_db uses DB difficulty, adds FSRS writeback (P0-08, P1-#12, P2-MEM-05)"
```

---

### Task 7: P0-10 + P0-11 — QQ C2C 流式配额 + SharedBlackboardDB asyncio.Lock

**Files:**
- Modify: `qq_bot_adapter.py` (C2C streaming quota check)
- Modify: `agent_core/shared_blackboard.py` or relevant DB variant

**Note:** These require reading the actual code to determine exact fix locations. The spec provides guidance but the implementation details depend on current code structure.

- [ ] **Step 1: Read relevant code sections**

Search for C2C streaming logic in `qq_bot_adapter.py` and SharedBlackboardDB in the codebase.

- [ ] **Step 2: Implement fixes per spec**

Apply the fixes described in the P0 spec for these two bugs.

- [ ] **Step 3: Run tests and commit**

```bash
git add qq_bot_adapter.py
git commit -m "fix: QQ C2C streaming quota guard + SharedBlackboardDB lock fix (P0-10, P0-11)"
```

---

### Task 8: P0-12 + P0-13 — J-Space health 信号值域 + degradation 阈值

**Files:**
- Modify: J-Space cognitive layer files (behavioral_signal.py, degradation_strategy.py)

**Note:** These require reading the actual J-Space code. The spec provides guidance but exact file locations need verification.

- [ ] **Step 1: Read J-Space code**

Search for health signal and degradation_strategy in the codebase.

- [ ] **Step 2: Implement fixes per spec**

Apply the fixes described in the P0 spec for these two bugs.

- [ ] **Step 3: Run tests and commit**

```bash
git commit -m "fix: J-Space health signal value domain + degradation threshold (P0-12, P0-13)"
```

---

## Phase 2: P1 — Important Bugs (27 bugs)

### Task 9: P1 BUG-01 + BUG-04 — sub_agent_manager 超时事件 + 不可用早退事件

**Files:**
- Modify: `agent_core/sub_agent_manager.py`

- [ ] **Step 1: Fix BUG-01 — unify timeout event type**

In `_parallel_run_one`, change `SUB_FAILED` to `SUB_CANCELLED` and `"error"` key to `"reason"`.

- [ ] **Step 2: Fix BUG-04 — add SUB_FAILED event for unavailable agent**

Before the early return in `_dispatch_single_sub_agent`, emit `SUB_FAILED` event.

- [ ] **Step 3: Run tests and commit**

```bash
git commit -m "fix: sub_agent_manager timeout event unification + unavailable agent event (P1-01, P1-04)"
```

---

### Task 10: P1 BUG-02 — CancelToken asyncio.create_task 无 loop 保护

**Files:**
- Modify: `core/cancel_token.py:47-48`

- [ ] **Step 1: Fix CancelToken.__init__**

Replace direct `asyncio.create_task` with try/except RuntimeError pattern:

```python
    def __init__(self, timeout: float | None = 60.0) -> None:
        self._cancelled = False
        self._reason = ""
        self._timeout = timeout
        self._created_at = time.monotonic()
        self._timer_task: asyncio.Task | None = None
        if timeout is not None and timeout > 0:
            try:
                loop = asyncio.get_running_loop()
                self._timer_task = loop.create_task(self._timeout_watch())
            except RuntimeError:
                self._timer_task = None

    async def ensure_started(self) -> None:
        if self._timeout and self._timer_task is None and not self._cancelled:
            self._timer_task = asyncio.create_task(self._timeout_watch())
```

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: CancelToken graceful init without running loop (P1-02)"
```

---

### Task 11: P1 BUG-03 — QQUser deliver 关键字参数调用

**Files:**
- Modify: `agent_core/user_qq.py:39`

- [ ] **Step 1: Change to positional arguments**

```python
# Before: await self._reply_fn(content=content, msg_seq=self._msg_seq_fn())
# After:
await self._reply_fn(content, self._msg_seq_fn())
```

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: QQUser deliver positional args instead of kwargs (P1-03)"
```

---

### Task 12: P1 Bug #6 + #7 — FluidMemory 兼容层修复

**Files:**
- Modify: `memory/fluid_memory.py`

- [ ] **Step 1: Fix score() to accept optional fsrs_state**

```python
    def score(self, similarity: float, created_at: float,
              access_count: int = 0, peak_weight: float = 1.0,
              fsrs_state: MemoryState | None = None) -> float:
        if fsrs_state is not None:
            R = fsrs_state.retrievability(time.time())
            return similarity * R
        # ... existing fallback logic ...
```

- [ ] **Step 2: Fix is_permanent() to delegate to FSRSModel**

```python
    def is_permanent(self, access_count: int,
                     fsrs_model: FSRSModel | None = None,
                     state: MemoryState | None = None) -> bool:
        if fsrs_model is not None and state is not None:
            new_phase = fsrs_model.transition(state, time.time())
            return new_phase == MemoryPhase.PERMANENT
        return access_count >= self.PERMANENT_ACCESS_THRESHOLD
```

- [ ] **Step 3: Run tests and commit**

```bash
git commit -m "fix: FluidMemory compat layer FSRS state support (P1-#6, P1-#7)"
```

---

### Task 13: P1 Bug #9 + #10 + #11 — memory_manager FSRS 优化

**Files:**
- Modify: `memory/memory_manager.py`

- [ ] **Step 1: Cache FSRSModel instance**

Add `self._fsrs = FSRSModel()` to `__init__` and use it everywhere instead of creating new instances.

- [ ] **Step 2: Fix encode_memory to set FSRS fields** (already done in Task 5)

- [ ] **Step 3: Fix _apply_fsrs_scoring last_review fallback** (already done in Task 5)

- [ ] **Step 4: Run tests and commit**

```bash
git commit -m "fix: memory_manager FSRS instance caching + init fixes (P1-#9, P1-#10, P1-#11)"
```

---

### Task 14: P1 Bug #13 — auto_link 每条边单独 commit O(N²)

**Files:**
- Modify: `db/db_concept.py`

- [ ] **Step 1: Batch commit for auto_link**

Add `auto_commit=False` parameter to edge operations in auto_link, then commit once at the end.

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: auto_link batch commit instead of per-edge commit (P1-#13)"
```

---

### Task 15: Remaining P1 bugs (J-Space + Channel)

**Files:**
- Various J-Space and channel adapter files

**Note:** These require reading the actual code. The P1 spec covers 7 J-Space + 7 channel bugs.

- [ ] **Step 1: Read and fix each bug per spec**

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: remaining P1 J-Space + channel bugs (P1-#14 through P1-#27)"
```

---

## Phase 3: P2 — Minor Bugs (33 bugs)

### Task 16: P2-CORE-01 through P2-CORE-07

**Files:**
- `agent_core/sub_agent_manager.py`
- `core/event_bus.py`
- `core/router_engine.py`
- `core/cancel_token.py`
- `belief_router.py`

- [ ] **Step 1: Fix each P2-CORE bug per spec**

Key fixes:
- P2-CORE-01: `("xiaoli", "xiaoli")` → `"xiaoli"`
- P2-CORE-02: `gen_task_id` use input_hint for traceable IDs
- P2-CORE-03: Cache regex patterns in RouterEngine
- P2-CORE-04: Deduplicate mention targets
- P2-CORE-05: Expose `compressed_summary` property
- P2-CORE-06: Separate `is_cancelled` (pure) from `check()` (with side effects)
- P2-CORE-07: Track `run_in_executor` Future

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: P2 core dispatch layer bugs (P2-CORE-01 through P2-CORE-07)"
```

---

### Task 17: P2-MEM-01 through P2-MEM-11

**Files:**
- `memory/fsrs_model.py`
- `memory/fluid_memory.py`
- `memory/memory_manager.py`
- `db/database.py`
- `core/dream_consolidation.py`
- `memory/confirm_correct.py`

- [ ] **Step 1: Fix each P2-MEM bug per spec**

Key fixes:
- P2-MEM-01: `MemoryPhase.safe()` classmethod
- P2-MEM-02: `difficulty_factor = max(0.0, (10.0 - D) / 9.0)` (already done in Task 4)
- P2-MEM-03: FluidMemory.score() FSRS path ignores peak_weight
- P2-MEM-04: v16 migration backfill (already done in Task 5)
- P2-MEM-05: `m.strength = max(m.strength * 0.95, R)` (already done in Task 6)
- P2-MEM-06: Sync `_memories` in consolidate_from_db
- P2-MEM-07: v15 migration idempotency
- P2-MEM-08: confirm_correct correct() new node FSRS fields
- P2-MEM-09 through P2-MEM-11: Various memory fixes

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: P2 memory system bugs (P2-MEM-01 through P2-MEM-11)"
```

---

### Task 18: P2 J-Space + Channel bugs

**Files:**
- Various J-Space and channel adapter files

- [ ] **Step 1: Fix each P2 J-Space + Channel bug per spec**

- [ ] **Step 2: Run tests and commit**

```bash
git commit -m "fix: P2 J-Space + channel bugs"
```

---

## Phase 4: Quality Score Improvements (7.04→7.84)

### Task 19: Quality improvements per spec

**Files:**
- Various modules as identified in quality_score_spec.md

- [ ] **Step 1: Implement quality improvements**

Focus on the highest-impact improvements identified in the spec:
- Interface design improvements (reduce parameter counts)
- Async safety improvements (lock patterns, ContextVar usage)
- Documentation/docstring coverage

- [ ] **Step 2: Run full test suite**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git commit -m "quality: interface + async + doc improvements (7.04→7.84)"
```

---

## Final Verification

### Task 20: Full integration test + cleanup

- [ ] **Step 1: Run complete test suite**

Run: `cd f:\naxida\xiaoda-agent && python -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 2: Verify no regressions in RAG pipeline**

Run the E2E RAG pipeline simulation script.

- [ ] **Step 3: Clean up temporary spec files**

Remove `docs/fix_spec_P0.md`, `docs/fix_spec_P1.md`, `docs/fix_spec_P2.md`, `docs/quality_score_spec.md` if they were copied for reference.

- [ ] **Step 4: Final commit and push**

```bash
git push origin main
```