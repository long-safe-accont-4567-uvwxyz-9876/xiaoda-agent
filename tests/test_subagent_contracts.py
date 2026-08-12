import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError

import pytest
from loguru import logger

from agent_core.subagents import (
    InvalidSubAgentInvocation,
    SubAgentInvocation,
    SubAgentInvocationResult,
)
from agent_dispatcher import ResourceBackend


@contextmanager
def _capture_loguru():
    records = []
    sink_id = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


class FakeResourceBackend:
    def read(self, path: str) -> str:
        return path

    def write(self, path: str, content: str) -> None:
        return None

    def edit(self, path: str, old: str, new: str) -> None:
        return None

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str, path: str = "/") -> list[str]:
        return [pattern, path]


class IncompleteResourceBackend:
    def read(self, path: str) -> str:
        return path


def test_resource_backend_is_runtime_checkable():
    assert isinstance(FakeResourceBackend(), ResourceBackend)
    assert not isinstance(IncompleteResourceBackend(), ResourceBackend)


def test_invocation_normalizes_and_deduplicates_safe_fields():
    invocation = SubAgentInvocation(
        target=" xiaoke ",
        task=" 调研这个问题 ",
        context=" 必要背景 ",
        allowed_tools=("web_search", "web_search", "read_file"),
        allowed_paths=("docs/**", "docs/**"),
        forbidden_paths=("**/*.env",),
        timeout_seconds=30,
    )

    assert invocation.target == "xiaoke"
    assert invocation.task == "调研这个问题"
    assert invocation.context == "必要背景"
    assert invocation.allowed_tools == ("web_search", "read_file")
    assert invocation.allowed_paths == ("docs/**",)
    assert invocation.forbidden_paths == ("**/*.env",)


def test_structured_invocation_defaults_to_no_tool_access():
    invocation = SubAgentInvocation(target="xiaoke", task="调研")

    assert invocation.allowed_tools == ()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"target": "Xiao-Ke"}, "target"),
        ({"task": "   "}, "task"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("inf")}, "timeout_seconds"),
        ({"allowed_tools": "read_file"}, "allowed_tools"),
        ({"allowed_tools": {"read_file", "web_search"}}, "allowed_tools"),
        ({"allowed_paths": ("../secret",)}, "allowed_paths"),
        ({"forbidden_paths": ("/etc/**",)}, "forbidden_paths"),
        ({"allowed_paths": ("C:/Users/secret",)}, "allowed_paths"),
        ({"allowed_paths": ("~/.ssh/**",)}, "allowed_paths"),
        ({"allowed_paths": ("safe\x00path",)}, "allowed_paths"),
        ({"permission_mode": []}, "permission_mode"),
        ({"request_id": 123}, "request_id"),
    ],
)
def test_invocation_rejects_invalid_or_unsafe_input(overrides, field):
    values = {"target": "xiaoke", "task": "调研"}
    values.update(overrides)

    with pytest.raises(InvalidSubAgentInvocation) as exc_info:
        SubAgentInvocation(**values)

    assert exc_info.value.field == field


def test_invocation_contract_excludes_parent_private_state():
    fields = set(SubAgentInvocation.__dataclass_fields__)

    assert "messages" not in fields
    assert "todos" not in fields
    assert "approval_state" not in fields
    assert "memory_state" not in fields


def test_invocation_accepts_existing_unicode_agent_identifiers():
    invocation = SubAgentInvocation(target="研究员", task="调研")

    assert invocation.target == "研究员"


def test_invocation_is_immutable():
    invocation = SubAgentInvocation(target="xiaoke", task="调研")

    with pytest.raises(FrozenInstanceError):
        invocation.task = "篡改"


def test_result_only_exposes_final_report_and_status():
    result = SubAgentInvocationResult.completed(target="xiaoke", final_report="结论")

    assert result.status == "completed"
    assert result.final_report == "结论"
    assert set(result.__dataclass_fields__) == {
        "target",
        "status",
        "final_report",
        "error_code",
        "error_message",
        "elapsed_ms",
    }


@pytest.mark.parametrize(
    "values",
    [
        {"target": "Bad-Target", "status": "completed", "final_report": "结论"},
        {"target": "xiaoke", "status": "completed", "final_report": "   "},
        {"target": "xiaoke", "status": "failed", "final_report": "不应存在"},
        {"target": "xiaoke", "status": "failed", "error_message": "   "},
        {"target": "xiaoke", "status": "failed", "elapsed_ms": -1},
    ],
)
def test_result_rejects_inconsistent_or_invalid_state(values):
    with pytest.raises(InvalidSubAgentInvocation):
        SubAgentInvocationResult(**values)


@pytest.mark.asyncio
async def test_dispatch_rejects_invalid_invocation_before_agent_lookup():
    from agent_dispatcher import AgentDispatcher

    dispatcher = AgentDispatcher(tts=None)

    with pytest.raises(InvalidSubAgentInvocation):
        await dispatcher.dispatch("xiaoke", "   ")


@pytest.mark.asyncio
async def test_legacy_dispatch_preserves_existing_unstructured_behavior():
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher, SubAgentConfig

    dispatcher = AgentDispatcher(tts=None)
    agent = MagicMock()
    agent.config = SubAgentConfig(
        name="xiaoke",
        display_name="小可",
        provider="test",
        model="test",
        allowed_paths=["docs/**"],
        forbidden_paths=["docs/private/**"],
        permission_mode="strict",
    )
    agent._filtered_tool_names.return_value = {"read_file", "web_search"}
    agent.chat = AsyncMock(return_value="最终报告")
    dispatcher._agents["xiaoke"] = agent

    result = await dispatcher.dispatch("xiaoke", " 调研 ", context=" 背景 ")

    assert result == "最终报告"
    assert "invocation" not in agent.chat.await_args.kwargs


@pytest.mark.asyncio
async def test_dispatch_invocation_accepts_structured_contract():
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher

    dispatcher = AgentDispatcher(tts=None)
    agent = MagicMock()
    agent.chat = AsyncMock(return_value="最终报告")
    dispatcher._agents["xiaoke"] = agent
    invocation = SubAgentInvocation(
        target="xiaoke",
        task=" 调研 ",
        context=" 背景 ",
        allowed_tools=("web_search",),
        allowed_paths=("docs/**",),
        permission_mode="strict",
        timeout_seconds=30,
    )

    result = await dispatcher.dispatch_invocation(invocation)

    assert result == SubAgentInvocationResult.completed(target="xiaoke", final_report="最终报告")
    agent.chat.assert_awaited_once_with(
        "调研",
        context="背景",
        status_callback=None,
        address_term="爸爸",
        extra_system_prompt="",
        invocation=invocation,
    )


@pytest.mark.asyncio
async def test_dispatch_invocation_maps_failure_to_structured_result():
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher

    dispatcher = AgentDispatcher(tts=None)
    agent = MagicMock()
    agent.chat = AsyncMock(side_effect=RuntimeError("secret internal detail"))
    dispatcher._agents["xiaoke"] = agent

    result = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研"))

    assert result.status == "failed"
    assert result.error_code == "SUB_AGENT_EXECUTION_FAILED"
    assert result.error_message == "agent invocation failed"


@pytest.mark.asyncio
async def test_dispatch_invocation_maps_unavailable_empty_timeout_and_cancellation():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher

    dispatcher = AgentDispatcher(tts=None)
    unavailable = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研"))
    assert unavailable.status == "unavailable"

    agent = MagicMock()
    agent.chat = AsyncMock(return_value="   ")
    dispatcher._agents["xiaoke"] = agent
    empty = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研"))
    assert empty.status == "failed"
    assert empty.error_code == "SUB_AGENT_EMPTY_RESULT"

    async def wait_forever(*args, **kwargs):
        await asyncio.sleep(1)

    agent.chat = AsyncMock(side_effect=wait_forever)
    timed_out = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研", timeout_seconds=0.01))
    assert timed_out.status == "timeout"

    agent.chat = AsyncMock(side_effect=asyncio.CancelledError())
    cancelled = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研"))
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_structured_invocation_enforces_tool_allowlist_at_execution():
    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = None
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("web_search",))
    call = ExtractedToolCall(id="1", name="read_file", arguments_json='{"path":"docs/a.md"}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "未授权" in result["content"]


@pytest.mark.asyncio
async def test_structured_invocation_blocks_nested_agent_messaging():
    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("send_message_to_agent",))
    call = ExtractedToolCall(id="1", name="send_message_to_agent", arguments_json='{"target_agent":"xiaolang","message":"执行"}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "禁止嵌套代理通信" in result["content"]


@pytest.mark.asyncio
async def test_structured_invocation_empty_allowlist_denies_all_tools():
    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    call = ExtractedToolCall(id="1", name="read_file", arguments_json='{"path":"docs/a.md"}')

    result = await agent._exec_one_tool_call(call, invocation=SubAgentInvocation(target="xiaoke", task="调研"))

    assert "未授权" in result["content"]


@pytest.mark.asyncio
async def test_structured_invocation_enforces_path_policy_before_execution():
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(
        target="xiaoke",
        task="调研",
        allowed_tools=("read_file",),
        allowed_paths=("docs/**",),
        forbidden_paths=("docs/private/**",),
    )
    call = ExtractedToolCall(id="1", name="read_file", arguments_json='{"path":"docs/private/a.md"}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "路径策略" in result["content"]
    agent._tool_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_path", ("docs/../secret/a.md", "/etc/passwd", "C:/Windows/system.ini", "~/.ssh/id_rsa"))
async def test_structured_invocation_rejects_unsafe_runtime_paths(unsafe_path):
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("read_file",), allowed_paths=("docs/**",))
    call = ExtractedToolCall(id="1", name="read_file", arguments_json=f'{{"path":"{unsafe_path}"}}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "路径策略" in result["content"]
    agent._tool_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_invocation_rejects_unsafe_path_without_configured_patterns():
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("read_file",))
    call = ExtractedToolCall(id="1", name="read_file", arguments_json='{"path":"/etc/passwd"}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "路径策略" in result["content"]
    agent._tool_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_path_denial_logs_safe_structured_details():
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(
        target="xiaoke",
        task="绝不能进入日志的任务",
        context="绝不能进入日志的背景",
        allowed_tools=("read_file",),
        allowed_paths=("docs/**",),
        request_id="request-log-1",
    )
    unsafe_path = "docs/../secret\n" + "x" * 300
    call = ExtractedToolCall(id="1", name="read_file", arguments_json=json.dumps({"path": unsafe_path}))

    with _capture_loguru() as records:
        await agent._exec_one_tool_call(call, invocation=invocation)

    record = next(record for record in records if record["message"] == "sub_agent.path_policy_denied")
    assert record["extra"]["target"] == "xiaoke"
    assert record["extra"]["request_id"] == "request-log-1"
    assert record["extra"]["tool"] == "read_file"
    assert record["extra"]["reason"] == "unsafe_path"
    assert "\n" not in record["extra"]["path"]
    assert len(record["extra"]["path"]) <= 200
    assert record["extra"]["allowed_pattern_count"] == 1
    rendered = str(record)
    assert "绝不能进入日志的任务" not in rendered
    assert "绝不能进入日志的背景" not in rendered


@pytest.mark.asyncio
async def test_invocation_timeout_logs_structured_details_without_content():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher, SubAgentConfig

    dispatcher = AgentDispatcher(tts=None)
    agent = MagicMock()
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test", memory_scope="isolated")

    async def wait_forever(*args, **kwargs):
        await asyncio.sleep(1)

    agent.chat = AsyncMock(side_effect=wait_forever)
    dispatcher._agents["xiaoke"] = agent
    invocation = SubAgentInvocation(
        target="xiaoke",
        task="绝不能进入日志的任务",
        context="绝不能进入日志的背景",
        timeout_seconds=0.01,
        request_id="request-timeout-1",
    )

    with _capture_loguru() as records:
        result = await dispatcher.dispatch_invocation(invocation)

    assert result.status == "timeout"
    record = next(record for record in records if record["message"] == "dispatcher.invocation_timeout")
    expected_extra = {
        "target": "xiaoke",
        "request_id": "request-timeout-1",
        "timeout_seconds": 0.01,
        "memory_scope": "isolated",
    }
    assert {key: record["extra"].get(key) for key in expected_extra} == expected_extra
    rendered = str(record)
    assert "绝不能进入日志的任务" not in rendered
    assert "绝不能进入日志的背景" not in rendered


def test_reasoning_prompt_only_exposes_invocation_allowed_tools():
    from agent_dispatcher import SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_executor = object()
    agent._core = None
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("web_search",))

    prompt = agent._build_dsml_tool_prompt(allowed_tools=set(invocation.allowed_tools))

    assert "web_search" in prompt
    assert "read_file" not in prompt


@pytest.mark.asyncio
async def test_dispatch_invocation_binds_and_restores_subagent_memory_scope():
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher, SubAgentConfig
    from memory.scope import Scope, bind_scope, current_scope, reset_scope

    dispatcher = AgentDispatcher(tts=None)
    agent = MagicMock()
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test", memory_scope="isolated")

    async def inspect_scope(*args, **kwargs):
        scope = current_scope()
        assert scope.agent_id == "xiaoke"
        assert scope.session_id == "parent-session:xiaoke"
        assert scope.request_id == "child-request"
        return "最终报告"

    agent.chat = AsyncMock(side_effect=inspect_scope)
    dispatcher._agents["xiaoke"] = agent
    parent_token = bind_scope(Scope(user_id="user-1", session_id="parent-session", agent_id="xiaoda", request_id="request-1"))
    try:
        result = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研", request_id="child-request"))
        assert result.status == "completed"
        assert current_scope().agent_id == "xiaoda"
    finally:
        reset_scope(parent_token)


@pytest.mark.asyncio
async def test_shared_memory_scope_preserves_parent_agent_scope():
    from unittest.mock import AsyncMock, MagicMock

    from agent_dispatcher import AgentDispatcher, SubAgentConfig
    from memory.scope import Scope, bind_scope, current_scope, reset_scope

    dispatcher = AgentDispatcher(tts=None)
    agent = MagicMock()
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test", memory_scope="shared")

    async def inspect_scope(*args, **kwargs):
        scope = current_scope()
        assert scope.agent_id == "xiaoda"
        assert scope.session_id == "parent-session"
        assert scope.request_id == "shared-request"
        return "最终报告"

    agent.chat = AsyncMock(side_effect=inspect_scope)
    dispatcher._agents["xiaoke"] = agent
    parent_token = bind_scope(Scope(user_id="user-1", session_id="parent-session", agent_id="xiaoda", request_id="parent-request"))
    try:
        result = await dispatcher.dispatch_invocation(SubAgentInvocation(target="xiaoke", task="调研", request_id="shared-request"))
        assert result.status == "completed"
        assert current_scope().request_id == "parent-request"
    finally:
        reset_scope(parent_token)


@pytest.mark.asyncio
async def test_structured_invocation_denies_file_tool_when_path_is_missing():
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("list_files",), allowed_paths=("docs/**",))
    call = ExtractedToolCall(id="1", name="list_files", arguments_json="{}")

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "缺少路径" in result["content"]
    agent._tool_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_invocation_applies_path_policy_to_document_reader():
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(target="xiaoke", task="调研", allowed_tools=("document_reader",), allowed_paths=("docs/**",))
    call = ExtractedToolCall(id="1", name="document_reader", arguments_json='{"path":"/etc/passwd"}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "路径策略" in result["content"]
    agent._tool_executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_invocation_strict_mode_denies_write_tool():
    from unittest.mock import AsyncMock

    from agent_dispatcher import ExtractedToolCall, SubAgent, SubAgentConfig

    agent = SubAgent.__new__(SubAgent)
    agent.config = SubAgentConfig(name="xiaoke", display_name="小可", provider="test", model="test")
    agent._tool_repair = None
    agent._tool_executor = AsyncMock()
    invocation = SubAgentInvocation(
        target="xiaoke",
        task="调研",
        allowed_tools=("write_file",),
        allowed_paths=("docs/**",),
        permission_mode="strict",
    )
    call = ExtractedToolCall(id="1", name="write_file", arguments_json='{"input_str":"docs/a.md|||content"}')

    result = await agent._exec_one_tool_call(call, invocation=invocation)

    assert "strict 模式拒绝" in result["content"]
    agent._tool_executor.execute.assert_not_awaited()
