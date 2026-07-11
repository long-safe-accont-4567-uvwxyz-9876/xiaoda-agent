# Memory & Cognition v0.6 Incremental Architecture Design

**Date:** 2026-07-10
**Status:** Approved
**Project:** xiaoda-agent

## 1. Goal

Improve memory accuracy, temporal correctness, associative recall, preference continuity, provenance, and cost/performance without replacing the existing SQLite/FTS5/sqlite-vec/KG stack.

The design incorporates useful mechanisms from mem0, Graphiti, Da7-Tech/mind, Agent_Memory_Techniques, and mazemaker, but does not copy their full architectures. Existing xiaoda-agent capabilities remain the foundation.

## 2. Constraints

- Preserve all original conversations, episodic memories, and KG records.
- Migrations must be idempotent, incremental, and reversible at the feature level.
- Original evidence is never physically deleted by automatic consolidation.
- Facts and preferences use full bitemporal versioning.
- Retrieval and retention scoring are separate.
- LLM extraction and conflict resolution have strict budgets, timeouts, and local fallbacks.
- Retrieval p95 must remain within 1.5x the measured baseline.
- Additional RSS must stay at or below 300 MB; spreading activation targets at most 50 MB.
- All existing tests must remain green; baseline is 1311 passed, 1 skipped.

## 3. Chosen Approach

Use an incremental cognitive-memory kernel:

1. Keep `episodic_memories`, FTS5, sqlite-vec, child chunks, and the existing KG.
2. Repair migration consistency and unsafe Dream behavior first.
3. Add bitemporal facts and preferences with normalized provenance.
4. Add typed memory edges for supersession, support, similarity, and bridges.
5. Add bounded one-hop spreading activation as a fifth retrieval channel.
6. Reinforce memories only after explicit answer citation or user confirmation.
7. Enable mechanisms only when offline A/B and Orange Pi benchmarks show net benefit.

Rejected for the first phase: full Graphiti service, Neo4j/FalkorDB, separate concept store, Hopfield network, ColBERT, GPU/PPR, community summarization, global LLM conflict scans, and seven-stage Dream processing.

## 4. Data Model

### 4.1 `memory_edges`

Represents relationships between episodic memories or derived records.

- `source_memory_id`
- `target_memory_id`
- `edge_type`: `supersedes`, `supports`, `similar`, `bridge`
- `weight`
- `confidence`
- `evidence_json`
- `created_at`
- `updated_at`

A unique constraint prevents duplicate typed edges.

### 4.2 `memory_facts`

Stores normalized facts without copying the original episode text.

- `id`
- `subject`
- `predicate`
- `object`
- `object_type`
- `valid_from`, `valid_to`: real-world valid-time interval
- `learned_at`, `expired_at`: system-time interval
- `status`: `active`, `superseded`, `rejected`, `uncertain`, `pending_review`
- `confidence`
- `fact_hash`
- `superseded_by`
- `created_at`, `updated_at`

Current facts satisfy `valid_to IS NULL`, `expired_at IS NULL`, and `status='active'`.

### 4.3 `memory_fact_sources`

Normalizes fact provenance:

- `fact_id`
- `memory_id`
- `evidence_text`
- `created_at`

A fact may have multiple source memories. Provenance is queryable and constrained by foreign keys.

### 4.4 `memory_preferences`

Preferences are first-class bitemporal objects:

- `id`
- `preference_key`
- `preference_value`
- `preference_type`
- `polarity`
- `scope`
- `valid_from`, `valid_to`
- `learned_at`, `expired_at`
- `status`
- `confidence`
- `observed_count`
- `explicitness`: `explicit` or `inferred`
- `superseded_by`
- `created_at`, `updated_at`

Explicit preferences can become active from one high-confidence statement. Inferred preferences require at least two independent evidence records.

### 4.5 `memory_preference_sources`

Normalizes preference provenance and supports confidence recomputation after evidence rejection.

### 4.6 KG Evolution

Continue using `knowledge_entities` and `knowledge_relations`.

- Treat legacy zero timestamps as unknown, not Unix epoch.
- Use valid-time and system-time fields in writes and reads.
- Add normalized source mapping.
- Add a canonical triple/fact hash unique constraint to stop random-ID duplicates.
- Current retrieval only traverses currently valid facts unless the query explicitly asks for history.

## 5. Bitemporal Semantics

- `valid_from`/`valid_to`: when a fact or preference is true in the user's world.
- `learned_at`/`expired_at`: when the agent knew or stopped trusting it.
- `NULL` means unknown/open-ended; zero is not used as unknown in new records.
- If a new fact has a known effective time, supersession closes the old valid interval at that time.
- If effective time is unknown, only the old system interval is closed.
- Complementary facts remain active together.
- Ambiguous conflict results stay visible as `uncertain` but are excluded from the definite-current view.

Public query interfaces include current views and `as_of(valid_time, known_at)` reconstruction for facts and preferences.

## 6. Write Pipeline

```text
raw episode
→ deterministic memory gate
→ one batched LLM extraction for facts/preferences/entities/time
→ normalization and exact hash dedupe
→ retrieve top-3 related prior versions
→ classify duplicate/complementary/superseding/historical/uncertain/rejected
→ bitemporal write
→ provenance and memory-edge write
→ asynchronous FTS/vector/KG/association refresh
```

The raw episode is committed first. Derived processing failures never lose or block the original memory.

Conflict resolution escalates by cost:

1. explicit correction/negation/time rules;
2. structured subject-predicate comparison;
3. top-3 FTS/vector candidates;
4. one LLM decision only when deterministic methods are insufficient;
5. on failure, add `pending_review` without automatically expiring old versions.

## 7. Retrieval Pipeline

```text
query
→ intent and temporal-range parsing
→ FTS + vector + KG + child-chunk retrieval in parallel
→ current/as-of bitemporal filtering
→ bounded memory-edge spreading channel
→ weighted RRF
→ normalized feature reranking
→ contradiction and diversity constraints
→ prompt-budget assembly with internal citation IDs
→ post-answer citation confirmation
```

Existing retrieval remains the fallback and baseline.

### 7.1 Bounded Spreading Activation

- Seeds: top 5–10 candidates from existing channels.
- Default: one hop.
- Two hops are disabled until A/B acceptance.
- Expand only top-degree/top-weight edges.
- Total candidate budget: 30–50.
- Propagation: `next = activation * edge_weight * decay / (hop + 1)`.
- `supports`, `similar`, `bridge`, shared-entity, and same-episode edges can propagate.
- `supersedes` never propagates as ordinary relevance; it controls version visibility and history.
- High-degree hubs receive a degree penalty.

### 7.2 Reranking Features

Normalize candidate features before fusion:

- semantic relevance
- lexical relevance
- entity/path relevance
- temporal validity
- fact/preference confidence
- source/evidence quality
- recency
- importance
- emotion affinity, hard-capped at 5–10%

Emotion may break close ties but may not outrank clearly more relevant neutral evidence.

## 8. Strict Reinforcement

Retrieval is read-only.

No reinforcement occurs because a memory was retrieved, reranked, or injected into the prompt. Reinforcement occurs only when:

- the answer explicitly cites the memory through an internal citation ID; or
- the user explicitly confirms that the memory is correct or helpful.

User correction, rejection, or negative feedback lowers confidence/edge weight or creates a superseding version. If actual use cannot be reliably established, no reinforcement occurs.

## 9. Dream and Retention

Repair existing behavior before adding advanced consolidation:

- Retention uses real importance rather than a fixed `similarity=0.5`.
- Retrieval relevance is never used as the permanent retention score.
- Prefix similarity cannot justify physical deletion.
- Duplicate/similar memories are connected and down-ranked, while original evidence remains recoverable.
- Derived summaries must contain evidence IDs, confidence, generator version, and active/superseded state.
- Offline consolidation has deterministic sampling, item limits, LLM-call limits, timeout, and transactional writes.

## 10. Evaluation

Create a deterministic offline dataset with at least 120 queries covering:

- direct and cross-session facts
- one-hop and two-hop associations
- explicit correction and implicit state change
- complementary non-conflicts
- current and historical facts
- late-arriving events
- explicit and inferred preferences
- emotional continuity
- Chinese aliases/entity ambiguity
- noisy hubs and negative examples
- provenance

Metrics:

- Recall@1/5, Precision@5, MRR, nDCG@5
- one-hop/two-hop recall and irrelevant-spread rate
- current, historical, and known-at accuracy
- supersession precision/recall and false-expiry rate
- provenance exact match
- preference accuracy, false inference, evidence coverage, revocation behavior
- p50/p95/p99, RSS, SQLite queries/write amplification
- prompt tokens and LLM call/token cost
- deterministic top-k Jaccard on fixed snapshots

Acceptance gates:

- composite retrieval score improves;
- current and historical accuracy do not regress;
- explicit correction accuracy ≥95%;
- false supersession <2%;
- provenance exact match ≥99%;
- two-hop requires ≥5% recall gain and ≤10% irrelevant-rate increase;
- p95 ≤1.5x baseline;
- extra RSS ≤300 MB;
- offline cycle ≤30 seconds;
- average new LLM calls ≤1 per turn, maximum 2 on complex conflict turns.

## 11. Rollout

Feature flags independently control:

- bitemporal extraction and filtering
- preference versioning
- memory edges
- one-hop/two-hop spreading
- strict citation reinforcement
- emotion reranking
- derived-memory generation

Fallback chain:

```text
full bitemporal + five channels + reranker
→ deterministic conflict + bitemporal + five channels
→ FTS + KG + spreading
→ existing FTS + vector
→ complete legacy retrieval path
```

## 12. Phase 1 Deliverables

- Evaluation harness and fixed dataset.
- Migration consistency diagnostics.
- Safe Dream scoring and non-destructive consolidation.
- Full bitemporal facts and preferences.
- Provenance and supersession edges.
- Current and historical query APIs.
- Retrieval version filtering.
- Strict citation/user-confirmation reinforcement.
- Experimental one-hop spreading channel.
- Feature flags, failure degradation, and rollback.

Not enabled by default in Phase 1: two-hop spreading, automatic bridge generation, community discovery, Hopfield, ColBERT, PPR/GPU, global conflict scans, seven-stage Dream, or deletion of original evidence.
