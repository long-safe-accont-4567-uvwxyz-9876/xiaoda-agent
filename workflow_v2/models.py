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
    MODEL = "model"
    SKILL = "skill"
    AGENT = "agent"
    WORKFLOW = "workflow"
    TRANSFORM = "transform"
    INPUT = "input"
    APPROVAL = "approval"
    DELAY = "delay"
    LEGACY_PROMPT = "legacy_prompt"  # migrated v1 'custom' nodes


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
