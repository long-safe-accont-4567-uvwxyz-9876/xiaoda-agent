# Subagent Isolation Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe structured warning logs for subagent path-policy denials and invocation timeouts.

**Architecture:** Keep policy enforcement and logging co-located at the decision branches in `agent_dispatcher.py`. Use a small path-sanitizing helper so every denial emits consistent bounded fields without logging task or context data.

**Tech Stack:** Python 3.11, Loguru, pytest

## Global Constraints

- Do not log task text, context, system prompts, file contents, or complete tool arguments.
- Sanitize CR, LF, and NUL from logged paths and cap them at 200 characters.
- Preserve existing return values and error codes.
- Do not add dependencies.

---

### Task 1: Failing Log Contract Tests

**Files:**
- Modify: `tests/test_subagent_contracts.py`

**Interfaces:**
- Consumes: Loguru sink callbacks and existing `SubAgentInvocation`
- Produces: executable requirements for `sub_agent.path_policy_denied` and `dispatcher.invocation_timeout`

- [ ] Add a sink-capture helper and tests for path denial reason/fields, sanitization/truncation, timeout fields, and sensitive task/context exclusion.
- [ ] Run `.venv/bin/pytest tests/test_subagent_contracts.py -q` and confirm the new assertions fail because the events are absent.

### Task 2: Structured Logging

**Files:**
- Modify: `agent_dispatcher.py:732-780`
- Modify: `agent_dispatcher.py:1092-1122`
- Test: `tests/test_subagent_contracts.py`

**Interfaces:**
- Produces: `_safe_log_path(path: str) -> str`
- Produces: `sub_agent.path_policy_denied` warning with fixed fields
- Produces: `dispatcher.invocation_timeout` warning with fixed fields

- [ ] Add `_safe_log_path` that replaces CR/LF/NUL with spaces and returns at most 200 characters.
- [ ] Emit a path denial warning immediately before each path-policy rejection return.
- [ ] Emit an invocation timeout warning in the `TimeoutError` branch without logging task or context.
- [ ] Run `.venv/bin/pytest tests/test_subagent_contracts.py -q` and confirm all contract tests pass.

### Task 3: Regression Verification

**Files:**
- Verify: `agent_dispatcher.py`
- Verify: `tests/test_subagent_contracts.py`

**Interfaces:**
- Preserves: existing dispatcher, path-policy and structured result behavior

- [ ] Run the subagent contract, path whitelist, event, timeout, parallel dispatch and WebUI regression suite.
- [ ] Run Ruff, Python compilation and `git diff --check` for the modified files.
