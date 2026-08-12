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
