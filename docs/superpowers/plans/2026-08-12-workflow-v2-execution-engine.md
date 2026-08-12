# Workflow V2 Execution Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current fake "workflow = Markdown skill prompt" mechanism with a real, persistent, deterministic DAG execution engine (Definition/Revision/Run/StepRun/RunEvent) that supports parallel/join, cooperative cancel, conservative crash recovery, and reliable WebUI event sync.

**Architecture:** Persistent state in SQLite is the single source of truth. A scheduler leases `ready` nodes via CAS, executes them through the unified Tool Invocation Pipeline, and commits each state transition together with a monotonic `seq` RunEvent in one transaction. WebSocket only notifies; REST snapshot + event replay reconcile. Immutable Revisions are validated at publish time; V1 workflows are migrated read-only via a compat converter.

**Tech Stack:** Python 3.11+, FastAPI, aiosqlite (SQLite), Pydantic v2, Loguru, pytest / pytest-asyncio.

## Global Constraints

- All workflow APIs require authentication (`Depends(get_current_user)`); no anonymous access.
- Revisions are immutable once published; a Run always binds one fixed `revision_id`.
- Every state transition (Run/Step) and its RunEvent commit in ONE SQLite transaction.
- RunEvent uniqueness/order: `UNIQUE(run_id, seq)`, `seq` strictly monotonic per run.
- Conservative recovery: on restart, leftover `running` steps fail; only `waiting_input` and steps with explicit `idempotency.mode="required"` may resume.
- Secrets appear only as `secret_ref`; plaintext never written to Revision/StepRun/RunEvent/snapshot/WebSocket.
- No arbitrary cycles in V2 v1; retries are node policy, not graph loops.
- Response bodies use existing `web.schemas.Envelope`.
- SQLite schema changes bump `CURRENT_SCHEMA_VERSION` in `db/database.py` (currently 26) and add a migration.

---

## File Structure

- `workflow_v2/__init__.py` — package exports.
- `workflow_v2/models.py` — Pydantic models: `WorkflowDefinition`, `WorkflowRevision`, `NodeSpec`, `EdgeSpec`, `WorkflowRun`, `WorkflowStepRun`, `WorkflowRunEvent`, all enums.
- `workflow_v2/graph.py` — DAG validation (cycles, unreachable, dangling edges, unclosed parallel/join), `content_hash`.
- `workflow_v2/refs.py` — `$ref` / `secret_ref` resolution against run input + completed step outputs (no eval/exec).
- `workflow_v2/repository.py` — atomic SQLite repository: CAS transitions + event append in one transaction.
- `workflow_v2/scheduler.py` — ready-set computation, lease/CAS claim, dispatch loop, retry/failure policy, join strategies, cancel propagation, conservative recovery.
- `workflow_v2/executors.py` — node executors (`tool`/`mcp`/`agent`/`workflow`/`transform`/`condition`/`input`/`approval`/`delay`) routing through the unified Tool Invocation Pipeline.
- `workflow_v2/events.py` — event Envelope builder, sanitizer, WebSocket publisher.
- `workflow_v2/migrate.py` — V1 JSON → V2 Revision compat converter (read-only on V1).
- `db/db_workflow.py` — schema DDL + query methods for the new tables.
- `web/routers/workflows_v2.py` — Definition/Revision/Run/signal/cancel REST + `/stream` WS.
- Tests under `tests/workflow_v2/`.

---

## Task 1: Domain models & enums

**Files:**
- Create: `workflow_v2/__init__.py`
- Create: `workflow_v2/models.py`
- Test: `tests/workflow_v2/test_models.py`

**Interfaces:**
- Produces: `RunStatus`, `StepStatus`, `NodeType`, `JoinStrategy`, `FailurePolicy` (str Enums); `NodeSpec`, `EdgeSpec`, `WorkflowRevision`, `WorkflowDefinition`, `WorkflowRun`, `WorkflowStepRun`, `WorkflowRunEvent` (Pydantic v2 `BaseModel`).

- [ ] **Step 1: Write the failing test**

```python
# tests/workflow_v2/test_models.py
from workflow_v2.models import (
    RunStatus, StepStatus, NodeType, JoinStrategy, FailurePolicy,
    NodeSpec, EdgeSpec, WorkflowRevision, WorkflowRun, WorkflowStepRun, WorkflowRunEvent,
)


def test_run_status_values():
    assert {s.value for s in RunStatus} == {
        "queued", "running", "waiting_input", "paused",
        "cancelling", "succeeded", "failed", "cancelled",
    }


def test_step_status_values():
    assert {s.value for s in StepStatus} == {
        "pending", "ready", "running", "waiting_input",
        "succeeded", "failed", "cancelled", "skipped",
    }


def test_node_spec_defaults():
    n = NodeSpec(id="a", type=NodeType.TOOL, name="A", config={"tool_ref": "t"})
    assert n.failure_policy == FailurePolicy.FAIL_RUN
    assert n.timeout_seconds == 60
    assert n.retry_policy.max_attempts == 1


def test_run_event_requires_seq():
    ev = WorkflowRunEvent(run_id="r1", seq=1, event_type="run_started", run_status=RunStatus.RUNNING)
    assert ev.seq == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/workflow_v2/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'workflow_v2'`

- [ ] **Step 3: Write minimal implementation**

```python
# workflow_v2/__init__.py
from .models import (  # noqa: F401
    RunStatus, StepStatus, NodeType, JoinStrategy, FailurePolicy,
    NodeSpec, EdgeSpec, RetryPolicy, WorkflowRevision, WorkflowDefinition,
    WorkflowRun, WorkflowStepRun, WorkflowRunEvent,
)
```

```python
# workflow_v2/models.py
from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class NodeType(str, Enum):
    START = "start"
    END = "end"
    CONDITION = "condition"
    PARALLEL = "parallel"
    JOIN = "join"
    TOOL = "tool"
    MCP = "mcp"
    AGENT = "agent"
    WORKFLOW = "workflow"
    TRANSFORM = "transform"
    INPUT = "input"
    APPROVAL = "approval"
    DELAY = "delay"
    LEGACY_PROMPT = "legacy_prompt"


class JoinStrategy(str, Enum):
    ALL_SUCCESS = "all_success"
    ALL_SETTLED = "all_settled"
    ANY_SUCCESS = "any_success"


class FailurePolicy(str, Enum):
    FAIL_RUN = "fail_run"
    CONTINUE = "continue"
    ROUTE_TO = "route_to"


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    backoff: str = "none"  # none | fixed | exponential
    retry_on: list[str] = Field(default_factory=list)


class Idempotency(BaseModel):
    mode: str = "none"  # none | required
    key: str | None = None


class NodeSpec(BaseModel):
    id: str
    type: NodeType
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 60
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_RUN
    route_to: str | None = None
    idempotency: Idempotency = Field(default_factory=Idempotency)


class EdgeSpec(BaseModel):
    source: str
    target: str
    condition: str | None = None  # branch label for condition nodes


class WorkflowRevision(BaseModel):
    revision_id: str
    workflow_id: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    input_schema: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    created_at: float = 0.0


class WorkflowDefinition(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    enabled: bool = True
    current_revision_id: str | None = None
    etag: str = ""


class WorkflowRun(BaseModel):
    run_id: str
    workflow_id: str
    revision_id: str
    status: RunStatus = RunStatus.QUEUED
    lock_version: int = 0
    parent_run_id: str | None = None
    idempotency_key: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    cancel_requested_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class WorkflowStepRun(BaseModel):
    run_id: str
    node_id: str
    attempt: int = 1
    status: StepStatus = StepStatus.PENDING
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None


class WorkflowRunEvent(BaseModel):
    run_id: str
    seq: int
    event_type: str
    run_status: RunStatus
    step_id: str | None = None
    attempt: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = 0.0
    schema_version: int = 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/workflow_v2/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add workflow_v2/__init__.py workflow_v2/models.py tests/workflow_v2/test_models.py
git commit -m "feat(workflow-v2): add domain models and enums"
```

---

## Task 2: DAG validation & content hash

**Files:**
- Create: `workflow_v2/graph.py`
- Test: `tests/workflow_v2/test_graph.py`

**Interfaces:**
- Consumes: `NodeSpec`, `EdgeSpec`, `NodeType` from `workflow_v2.models`.
- Produces: `validate_graph(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> None` (raises `GraphError` with `.code` and `.details`); `compute_content_hash(nodes, edges, input_schema) -> str`; exception class `GraphError(Exception)` with attrs `code: str`, `details: dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/workflow_v2/test_graph.py
import pytest
from workflow_v2.models import NodeSpec, EdgeSpec, NodeType
from workflow_v2.graph import validate_graph, compute_content_hash, GraphError


def _n(nid, ntype=NodeType.TOOL):
    return NodeSpec(id=nid, type=ntype, name=nid, config={"tool_ref": "t"})


def test_valid_linear_graph_passes():
    nodes = [_n("start", NodeType.START), _n("a"), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    validate_graph(nodes, edges)  # no raise


def test_cycle_rejected():
    nodes = [_n("start", NodeType.START), _n("a"), _n("b"), _n("end", NodeType.END)]
    edges = [
        EdgeSpec(source="start", target="a"),
        EdgeSpec(source="a", target="b"),
        EdgeSpec(source="b", target="a"),
    ]
    with pytest.raises(GraphError) as exc:
        validate_graph(nodes, edges)
    assert exc.value.code == "WORKFLOW_REVISION_INVALID"
    assert "cycle" in exc.value.details


def test_unreachable_node_rejected():
    nodes = [_n("start", NodeType.START), _n("a"), _n("orphan"), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    with pytest.raises(GraphError) as exc:
        validate_graph(nodes, edges)
    assert "orphan" in exc.value.details.get("unreachable", [])


def test_dangling_edge_rejected():
    nodes = [_n("start", NodeType.START), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="ghost")]
    with pytest.raises(GraphError):
        validate_graph(nodes, edges)


def test_content_hash_stable_and_order_independent():
    nodes = [_n("start", NodeType.START), _n("a"), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    h1 = compute_content_hash(nodes, edges, {})
    h2 = compute_content_hash(list(reversed(nodes)), list(reversed(edges)), {})
    assert h1 == h2 and len(h1) == 64
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/workflow_v2/test_graph.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'workflow_v2.graph'`

- [ ] **Step 3: Write minimal implementation**

```python
# workflow_v2/graph.py
from __future__ import annotations
import hashlib
import json
from workflow_v2.models import NodeSpec, EdgeSpec, NodeType


class GraphError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = "WORKFLOW_REVISION_INVALID"
        self.details = details or {}


def validate_graph(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> None:
    ids = [n.id for n in nodes]
    id_set = set(ids)
    if len(ids) != len(id_set):
        raise GraphError("duplicate node id", {"duplicate": True})

    starts = [n.id for n in nodes if n.type == NodeType.START]
    ends = [n.id for n in nodes if n.type == NodeType.END]
    if len(starts) != 1:
        raise GraphError("exactly one start required", {"starts": starts})
    if not ends:
        raise GraphError("at least one end required", {"ends": ends})

    adj: dict[str, list[str]] = {i: [] for i in id_set}
    for e in edges:
        if e.source not in id_set or e.target not in id_set:
            raise GraphError("dangling edge", {"edge": [e.source, e.target]})
        adj[e.source].append(e.target)

    # reachability from start
    seen: set[str] = set()
    stack = [starts[0]]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj[cur])
    unreachable = sorted(id_set - seen)
    if unreachable:
        raise GraphError("unreachable nodes", {"unreachable": unreachable})

    # cycle detection (DFS colors)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in id_set}

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in adj[u]:
            if color[v] == GRAY:
                raise GraphError("cycle detected", {"cycle": [u, v]})
            if color[v] == WHITE:
                dfs(v)
        color[u] = BLACK

    dfs(starts[0])


def compute_content_hash(nodes: list[NodeSpec], edges: list[EdgeSpec], input_schema: dict) -> str:
    payload = {
        "nodes": sorted((n.model_dump(mode="json") for n in nodes), key=lambda x: x["id"]),
        "edges": sorted(([e.source, e.target, e.condition] for e in edges)),
        "input_schema": input_schema,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/workflow_v2/test_graph.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add workflow_v2/graph.py tests/workflow_v2/test_graph.py
git commit -m "feat(workflow-v2): add DAG validation and content hashing"
```

---
