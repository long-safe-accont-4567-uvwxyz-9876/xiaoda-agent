# Memory & Cognition v0.6 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use strict RED-GREEN-REFACTOR TDD. Every task runs focused tests before committing.

**Goal:** Deliver the first high-value, zero-data-loss memory improvements: bitemporal facts/preferences, safe Dream consolidation, and a measurable one-hop association retrieval prototype.

**Architecture:** Preserve episodic memories as immutable evidence. Add normalized derived tables and APIs beside the legacy path, controlled by feature flags. Keep retrieval/retention independent and integrate only after A/B evidence.

**Tech Stack:** Python 3.11, asyncio, aiosqlite, SQLite/FTS5/sqlite-vec, pytest.

## Global Constraints

- Original conversations, episodic memories, and KG records are never automatically physically deleted.
- Migrations are idempotent and existing schema v12 databases upgrade safely.
- Facts and preferences use valid time plus system time; NULL means unknown/open, not zero.
- Retrieval is read-only; only explicit answer citation or user confirmation reinforces memory.
- Existing 1311 passing tests must not regress.
- p95 must remain within 1.5x baseline and additional RSS within 300 MB.

---

### Task 1: Bitemporal schema and query APIs

**Files:**
- Modify: `db/database.py`
- Create: `db/db_temporal_memory.py`
- Create: `tests/test_bitemporal_memory.py`

**Interfaces:**
- `TemporalMemoryDB.get_current_facts(...)`
- `TemporalMemoryDB.get_facts_as_of(valid_time, known_at=None, ...)`
- `TemporalMemoryDB.get_current_preferences(...)`
- `TemporalMemoryDB.get_preferences_as_of(valid_time, known_at=None, ...)`
- `supersede_fact(...)`, `supersede_preference(...)`

- [ ] Write migration tests for fresh and v12 databases; verify RED.
- [ ] Add a v13 idempotent migration for facts, preferences, provenance, and typed memory edges.
- [ ] Write current/as-of fact tests; verify RED, implement, verify GREEN.
- [ ] Write current/as-of preference tests; verify RED, implement, verify GREEN.
- [ ] Write supersession/complementary/history tests and implement transactionally.
- [ ] Run focused tests and migration regression tests.
- [ ] Commit.

### Task 2: Safe Dream retention and non-destructive consolidation

**Files:**
- Modify: `core/dream_consolidation.py`
- Modify as needed: `db/db_memory.py`
- Create: `tests/test_dream_non_destructive.py`

**Interfaces:**
- Retention scoring consumes actual `importance`.
- Similar/duplicate consolidation returns relationships or down-ranking actions, never physical-delete IDs.

- [ ] Write regression test proving `consolidate_db()` must pass real importance; verify RED.
- [ ] Implement minimal fix and verify GREEN.
- [ ] Write test proving prefix-similar memories are not physically deleted; verify RED.
- [ ] Replace deletion with non-destructive marking/relationship output compatible with legacy DB.
- [ ] Verify archive behavior remains recoverable and existing Dream tests pass.
- [ ] Commit.

### Task 3: Evaluation harness and bounded one-hop association prototype

**Files:**
- Create: `memory/spreading_activation.py`
- Create: `evaluation/memory_benchmark.py`
- Create: `evaluation/datasets/memory_v06_cases.json`
- Create: `tests/test_spreading_activation.py`
- Create: `tests/test_memory_benchmark.py`

**Interfaces:**
- `spread_activation(seed_scores, adjacency, max_hops=1, decay=0.5, threshold=0.05, candidate_budget=50)`
- deterministic benchmark metrics for Recall@K, MRR, nDCG@K, irrelevant-spread rate, and latency percentiles.

- [ ] Write one-hop propagation/budget/hub-penalty tests; verify RED.
- [ ] Implement deterministic pure-Python spreading; verify GREEN.
- [ ] Write metric tests using a tiny fixed dataset; verify RED.
- [ ] Implement benchmark runner and at least 120 deterministic cases or a generator producing exactly 120 versioned cases.
- [ ] Run focused performance sanity test and focused unit tests.
- [ ] Commit.

### Task 4: Integration and gates

**Files:**
- Modify based on reviewed interfaces only.
- Add integration tests for feature flags and legacy fallback.

- [ ] Review and cherry-pick Tasks 1–3.
- [ ] Resolve interface conflicts without weakening constraints.
- [ ] Add feature flags defaulting experimental retrieval off.
- [ ] Run focused integration tests.
- [ ] Run `python3 -m pytest -q` and compare with 1311-pass baseline.
- [ ] Run benchmark and report quality, p95, RSS, and cost deltas.
- [ ] Commit only after fresh verification.
