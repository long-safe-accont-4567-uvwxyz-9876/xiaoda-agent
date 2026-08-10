# Route A Resource Backend And Subagent Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a storage-neutral resource protocol and an immutable, validated subagent invocation boundary while preserving existing dispatcher behavior.

**Architecture:** Keep the current Agent runtime and define the resource capability seam next to its primary consumer. Put invocation and result contracts in a focused `agent_core.subagents` module, validate at dispatcher ingress, and retain `str | None` compatibility until adapters migrate independently.

**Tech Stack:** Python 3.11, standard-library dataclasses and Protocol, pytest, Ruff

## Global Constraints

- Do not add LangChain or LangGraph dependencies.
- Do not expose host filesystem paths through the resource protocol.
- Do not include Shell execution in `ResourceBackend`.
- Do not propagate parent messages, todos, approval state, or private memory state.
- Preserve the existing `AgentDispatcher.dispatch()` return contract.

---

### Task 1: Contract Tests

**Files:**
- Create: `tests/test_subagent_contracts.py`

**Interfaces:**
- Consumes: `agent_dispatcher.ResourceBackend`
- Produces: executable behavior requirements for `SubAgentInvocation`, `SubAgentInvocationResult`, and dispatcher ingress validation

- [ ] Write tests for runtime protocol conformance, immutable input normalization, unsafe input rejection, private-state exclusion, final-report-only output, and pre-lookup dispatcher validation.
- [ ] Run `.venv/bin/pytest tests/test_subagent_contracts.py -q` and confirm collection fails because `agent_core.subagents` does not exist.

### Task 2: Resource Backend Protocol

**Files:**
- Modify: `agent_dispatcher.py`

**Interfaces:**
- Consumes: standard-library `Protocol` and `runtime_checkable`
- Produces: `ResourceBackend.read(path)`, `write(path, content)`, `edit(path, old, new)`, `glob(pattern)`, and `grep(pattern, path="/")`

- [ ] Define `ResourceBackend` in the existing protocol section without introducing a concrete filesystem implementation or execute capability.
- [ ] Run `.venv/bin/pytest tests/test_subagent_contracts.py::test_resource_backend_is_runtime_checkable -q` and confirm it passes.

### Task 3: Explicit Subagent DTOs

**Files:**
- Create: `agent_core/subagents.py`
- Test: `tests/test_subagent_contracts.py`

**Interfaces:**
- Produces: `InvalidSubAgentInvocation(field, reason)`, immutable `SubAgentInvocation`, and immutable `SubAgentInvocationResult`

- [ ] Implement normalization and validation for Unicode-compatible lowercase target identifiers, task, context, ordered tool collections, virtual path collections, permission mode, timeout, and request ID.
- [ ] Implement stable de-duplication and reject host absolute paths, backslashes, NUL, and parent traversal.
- [ ] Implement `SubAgentInvocationResult.completed(target, final_report, elapsed_ms=None)`.
- [ ] Run `.venv/bin/pytest tests/test_subagent_contracts.py -q` and confirm all contract tests pass.

### Task 4: Dispatcher Ingress

**Files:**
- Modify: `agent_dispatcher.py:985-998`
- Test: `tests/test_subagent_contracts.py`

**Interfaces:**
- Consumes: `SubAgentInvocation(target, task, context)`
- Preserves: `dispatch(name, task, context, status_callback, address_term, extra_system_prompt) -> str | None`

- [ ] Construct and validate the invocation before Agent lookup.
- [ ] Pass only normalized task and context into `SubAgent.chat()`.
- [ ] Keep unknown Agent behavior as `None` and retain the `dispatch = dispatch_single` alias.
- [ ] Add `dispatch_invocation()` to enforce invocation timeout and pass allowlists into the tool execution boundary while returning `SubAgentInvocationResult`.
- [ ] Run contract and existing dispatcher-related regression tests.

### Task 5: Design And Verification

**Files:**
- Create: `docs/superpowers/specs/2026-08-09-route-a-resource-subagent-design.md`

**Interfaces:**
- Produces: authoritative data-flow, validation, error handling, testing, compatibility, and non-goal documentation

- [ ] Document the ingress-to-result data flow and isolation boundary.
- [ ] Document stable validation and domain error mapping.
- [ ] Run `.venv/bin/pytest tests/test_subagent_contracts.py tests/test_sub_agent_path_whitelist.py tests/test_sub_agent_events.py tests/test_sub_agent_timeout.py tests/test_parallel_dispatch.py tests/test_webui_subagent_xp.py -q`.
- [ ] Run `.venv/bin/ruff check agent_dispatcher.py agent_core/subagents.py tests/test_subagent_contracts.py`.
- [ ] Inspect language diagnostics for all modified Python files and resolve new errors.
