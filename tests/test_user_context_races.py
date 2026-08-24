from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_context import AgentContext
from agent_core._shared import ProcessResult, RequestContext, UserIdentity
from agent_core.core import AgentCore
from agent_core.message_processor import MessageProcessorMixin
from agent_core.mixins.main_path import MainPathMixin
from agent_core.principal import Principal
from agent_core.tool_executor_mixin import ToolExecutorMixin
from core.background_tasks import BackgroundTaskManager
from core.bootstrap import AgentCoreBootstrapper


class _PausedNotebook:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_current_focus(self):
        self.started.set()
        await self.release.wait()
        return "ALICE_FOCUS"

    async def get_pending_tasks_summary(self):
        return ["ALICE_TASK"]


class _PausedPortrait:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ensure_exists(self, address_term=""):
        self.started.set()
        await self.release.wait()
        return "ALICE_PORTRAIT"


class _ToolHost(ToolExecutorMixin):
    pass


class _ResourceHost(ToolExecutorMixin):
    _call_with_timeout = MessageProcessorMixin._call_with_timeout
    _load_user_context_resources = MessageProcessorMixin._load_user_context_resources


class _MainPathHost(MainPathMixin):
    def _update_mental_state_emotion(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_notebook_load_finishing_after_switch_cannot_write_bob() -> None:
    context = AgentContext()
    await context.switch_user_context("alice")
    notebook = _PausedNotebook()
    host = _ToolHost()
    host.context = context
    host.notebook_manager = notebook

    load = asyncio.create_task(host._load_notebook_context())
    await notebook.started.wait()
    await context.switch_user_context("bob")
    notebook.release.set()
    await load

    assert context.notebook_focus is None
    assert context.pending_tasks is None
    await context.switch_user_context("alice")
    assert context.notebook_focus is None
    assert context.pending_tasks is None


@pytest.mark.asyncio
async def test_memory_retrieval_finishing_after_switch_cannot_write_bob() -> None:
    context = AgentContext()
    await context.switch_user_context("alice")
    started = asyncio.Event()
    release = asyncio.Event()
    host = _MainPathHost()
    host.context = context

    async def retrieve(*_args, **_kwargs):
        started.set()
        await release.wait()
        return [{"summary": "ALICE_MEMORY"}]

    host._retrieve_main_memories = retrieve
    request = SimpleNamespace(
        user_id="alice",
        last_user_emotion="",
        user_context_token=None,
    )

    setup = asyncio.create_task(
        host._setup_main_emotion_and_memory("hello", True, request)
    )
    await started.wait()
    await context.switch_user_context("bob")
    release.set()
    await setup

    assert context.memory_retrieval is None
    await context.switch_user_context("alice")
    assert context.memory_retrieval is None


@pytest.mark.asyncio
async def test_portrait_finishing_after_switch_cannot_write_bob() -> None:
    context = AgentContext()
    await context.switch_user_context("alice", address_term="Alice称谓")
    portrait = _PausedPortrait()
    manager = BackgroundTaskManager(
        MagicMock(), context, portrait_manager=portrait
    )

    task = asyncio.create_task(manager._portrait_cold_start())
    await portrait.started.wait()
    await context.switch_user_context("bob", address_term="Bob称谓")
    portrait.release.set()
    await task

    assert context.user_portrait is None
    await context.switch_user_context("alice", address_term="Alice称谓")
    assert context.user_portrait is None


@pytest.mark.asyncio
async def test_core_lock_switches_identity_term_before_processing() -> None:
    core = AgentCore.__new__(AgentCore)
    core.context = AgentContext()
    core._context_lock = asyncio.Lock()
    alice_started = asyncio.Event()
    release_alice = asyncio.Event()
    observed: list[tuple[str, str]] = []

    async def process_impl(ctx, *_args, **_kwargs):
        observed.append((ctx.user_id, core.context.current_address_term))
        if ctx.user_id == "alice":
            alice_started.set()
            await release_alice.wait()
        return ProcessResult(reply="ok")

    core._process_impl = process_impl
    alice_ctx = RequestContext(user_id="alice")
    alice_ctx.identity = UserIdentity(False, "Alice", "Alice称谓")
    bob_ctx = RequestContext(user_id="bob")
    bob_ctx.identity = UserIdentity(False, "Bob", "Bob称谓")
    # _process_impl_locked 在锁内用 ctx.conversation_session.activation_key
    # 激活共享 AgentContext；编排测试需提供与身份一致的会话桩。
    alice_ctx.conversation_session = SimpleNamespace(activation_key="alice")
    bob_ctx.conversation_session = SimpleNamespace(activation_key="bob")

    alice_task = asyncio.create_task(core._process_impl_locked(
        alice_ctx, "a", "alice", "qq", "", "", None, None
    ))
    await alice_started.wait()
    bob_task = asyncio.create_task(core._process_impl_locked(
        bob_ctx, "b", "bob", "qq", "", "", None, None
    ))
    await asyncio.sleep(0)
    release_alice.set()
    await asyncio.gather(alice_task, bob_task)

    assert observed == [("alice", "Alice称谓"), ("bob", "Bob称谓")]
    assert core.context._current_user_id == "bob"


@pytest.mark.asyncio
async def test_owner_restore_loads_resources_for_explicit_target_token() -> None:
    context = AgentContext()
    host = MagicMock()
    host.context = context
    host.db = None
    host._load_user_context_resources = AsyncMock()

    await MessageProcessorMixin._restore_user_context(
        host,
        "alice",
        address_term="Alice称谓",
        load_user_resources=True,
    )

    token = host._load_user_context_resources.await_args.args[0]
    assert token.user_id == "alice"
    assert token == context.get_user_context_token()
    assert context.current_address_term == "Alice称谓"


@pytest.mark.asyncio
async def test_first_user_resources_survive_initial_activation_and_reactivation() -> None:
    context = AgentContext()
    host = _ResourceHost()
    host.context = context
    host.db = MagicMock()
    host.db.get_conversations_readonly = AsyncMock(return_value=[{
        "user_message": "FIRST_USER_MESSAGE",
        "assistant_reply": "FIRST_USER_REPLY",
        "timestamp": 0,
    }])
    host.portrait_manager = MagicMock()
    host.portrait_manager.get_current_portrait = AsyncMock(return_value={
        "content": "FIRST_USER_PORTRAIT",
        "version": 1,
    })
    host.notebook_manager = MagicMock()
    host.notebook_manager.get_current_focus = AsyncMock(
        return_value="FIRST_USER_FOCUS"
    )
    host.notebook_manager.get_pending_tasks_summary = AsyncMock(
        return_value=["FIRST_USER_TASK"]
    )

    token = await MessageProcessorMixin._restore_user_context(
        host,
        "alice",
        address_term="Alice称谓",
        load_user_resources=True,
    )

    assert token == context.get_user_context_token()
    assert context.user_portrait == "FIRST_USER_PORTRAIT"
    assert context.notebook_focus == "FIRST_USER_FOCUS"
    assert context.pending_tasks == ["FIRST_USER_TASK"]
    assert "FIRST_USER_MESSAGE" in context._restored_summary

    await context.switch_user_context("bob", address_term="Bob称谓")
    await context.switch_user_context("alice", address_term="Alice称谓")

    assert context.user_portrait == "FIRST_USER_PORTRAIT"
    assert context.notebook_focus == "FIRST_USER_FOCUS"
    assert context.pending_tasks == ["FIRST_USER_TASK"]
    assert "FIRST_USER_MESSAGE" in context._restored_summary


@pytest.mark.asyncio
async def test_bootstrap_does_not_load_user_state_before_target_key_exists() -> None:
    core = MagicMock()
    core.context = AgentContext()
    core.learning_manager.get_system_prompt_additions = AsyncMock(return_value="RULES")
    core.instinct_manager = None
    core.portrait_manager.get_current_portrait = AsyncMock(
        return_value={"content": "UNBOUND_PORTRAIT", "version": 1}
    )
    core._load_notebook_context = AsyncMock()
    core.context.restore_from_db = AsyncMock()
    core.db = MagicMock()
    core.router = MagicMock()
    core.memory = MagicMock()
    core.notebook_manager = MagicMock()
    core.security = MagicMock()

    await AgentCoreBootstrapper(core)._init_interaction()

    assert core.context.learned_rules == "RULES"
    assert core.context.user_portrait is None
    core.portrait_manager.get_current_portrait.assert_not_awaited()
    core._load_notebook_context.assert_not_awaited()
    core.context.restore_from_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_resource_load_claim_is_single_and_initialized_is_sticky() -> None:
    context = AgentContext()
    token = await context.switch_user_context("alice")

    first, second = await asyncio.gather(
        context.claim_user_context_resources(token),
        context.claim_user_context_resources(token),
    )

    assert sorted((first, second)) == [False, True]
    assert await context.complete_user_context_resources(token) is True
    assert await context.claim_user_context_resources(token) is False

    await context.switch_user_context("bob")
    await context.switch_user_context("alice")
    assert await context.claim_user_context_resources(
        context.get_user_context_token()
    ) is False


@pytest.mark.asyncio
async def test_resource_load_failure_allows_next_request_retry() -> None:
    context = AgentContext()
    token = await context.switch_user_context("alice")

    assert await context.claim_user_context_resources(token) is True
    assert await context.fail_user_context_resources(token) is True
    assert await context.claim_user_context_resources(token) is True


@pytest.mark.asyncio
async def test_concurrent_first_requests_run_only_one_resource_loader() -> None:
    context = AgentContext()
    host = MagicMock()
    host.context = context
    host.db = None
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_resources(_token):
        started.set()
        await release.wait()
        return True

    host._load_user_context_resources = AsyncMock(side_effect=load_resources)
    first = asyncio.create_task(MessageProcessorMixin._restore_user_context(
        host,
        "alice",
        address_term="Alice称谓",
        load_user_resources=True,
    ))
    await started.wait()
    second = asyncio.create_task(MessageProcessorMixin._restore_user_context(
        host,
        "alice",
        address_term="Alice称谓",
        load_user_resources=True,
    ))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert host._load_user_context_resources.await_count == 1


@pytest.mark.asyncio
async def test_failed_resource_loader_retries_on_next_request() -> None:
    context = AgentContext()
    host = MagicMock()
    host.context = context
    host.db = None
    host._load_user_context_resources = AsyncMock(side_effect=[False, True])

    await MessageProcessorMixin._restore_user_context(
        host,
        "alice",
        load_user_resources=True,
    )
    await MessageProcessorMixin._restore_user_context(
        host,
        "alice",
        load_user_resources=True,
    )

    assert host._load_user_context_resources.await_count == 2
    assert context._resources_initialized is True


@pytest.mark.asyncio
async def test_old_activation_cannot_record_failure_for_new_user() -> None:
    context = AgentContext()
    alice_token = await context.switch_user_context("alice")
    await context.switch_user_context("bob")

    assert await context.record_failure(
        alice_token,
        "处理超时",
        "ALICE_PRIVATE_TIMEOUT",
    ) is False
    assert context.consume_failure() is None


@pytest.mark.asyncio
async def test_group_guest_does_not_load_real_owner_managers(tmp_path) -> None:
    from db.database import DatabaseManager
    from emotion.portrait_manager import PortraitManager
    from memory.notebook_manager import NotebookManager

    db = DatabaseManager(tmp_path / "guest-policy.db")
    await db.init()
    try:
        await db.memory.insert_portrait("OWNER_PORTRAIT", version=1)
        owner_notebook = NotebookManager(db=db, notebook=db.notebook, router=MagicMock())
        await owner_notebook.add_focus("OWNER_FOCUS")
        await owner_notebook.schedule_task("OWNER_TASK")
        await db.insert_conversation_log(
            user_id="qq_guest",
            source="qq_group",
            user_message="GUEST_HISTORY",
            assistant_reply="PRIVATE_REPLY",
        )

        host = _ResourceHost()
        host.context = AgentContext()
        host.db = db
        host.portrait_manager = PortraitManager(
            db=db,
            memory=db.memory,
            router=MagicMock(),
            notebook=db.notebook,
        )
        host.notebook_manager = owner_notebook
        host._tool_call_handler = None
        host.security = MagicMock()
        host.security.is_allowed.return_value = (True, "")
        guest_ctx = RequestContext(user_id="qq_guest", is_master=False)
        guest_ctx.identity = UserIdentity(False, "访客", "朋友")
        guest_ctx.principal = SimpleNamespace(is_owner=False)

        await MessageProcessorMixin._init_and_restore_context(
            host,
            guest_ctx,
            "访客问题",
            "qq_guest",
            "qq_group",
            None,
            "guest-openid",
            "group-session",
        )

        assert (await host.portrait_manager.get_current_portrait())["content"] == "OWNER_PORTRAIT"
        assert await owner_notebook.get_current_focus() == "OWNER_FOCUS"
        assert await owner_notebook.get_pending_tasks_summary()
        assert host.context.user_portrait is None
        assert host.context.notebook_focus is None
        assert host.context.pending_tasks is None
        assert host.context._restored_summary == ""
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_qq_timeout_uses_request_activation_token_after_switch() -> None:
    from qq_bot_adapter import AIQQBot, QQPipelineRequest

    context = AgentContext()
    alice_token = await context.switch_user_context("qq_alice")
    bot = AIQQBot.__new__(AIQQBot)
    bot.agent = SimpleNamespace(context=context)
    message = SimpleNamespace(reply=AsyncMock())
    request = QQPipelineRequest(
        text="ALICE_PRIVATE_TIMEOUT",
        user_id="qq_alice",
        source="qq_c2c",
        user_openid="alice-openid",
        message=message,
    )
    request.user_context_token = alice_token

    await context.switch_user_context("qq_bob")
    await bot._on_core_timeout(request)

    assert context.consume_failure() is None


def _web_dispatch_core() -> AgentCore:
    core = AgentCore.__new__(AgentCore)
    core.context = AgentContext()
    core._context_lock = asyncio.Lock()
    core._hook_engine = MagicMock()
    core._bg_task_manager = MagicMock()
    core.router = MagicMock()
    core.router.get_current_chat_model.return_value = {"model_id": "web-model"}
    core._resolve_principal = MagicMock(return_value=Principal(
        principal_id="webui",
        is_owner=True,
        display_name="爸爸",
        address_term="Web称谓",
    ))
    core._resolve_shared_context_id = MagicMock(return_value="web-context")
    return core


@pytest.mark.asyncio
async def test_web_direct_subagent_isolated_from_active_group_guest() -> None:
    core = _web_dispatch_core()
    await core.context.switch_user_context("qq_guest", address_term="朋友")
    core.context.history = [{"role": "user", "content": "GROUP_GUEST_HISTORY"}]
    observed: dict[str, object] = {}

    async def dispatch(
        _target,
        _text,
        *,
        user_id,
        source,
        session_id,
        trace,
        ctx,
    ):
        observed["token"] = ctx.user_context_token
        observed["user_id"] = user_id
        observed["source"] = source
        observed["history_before"] = list(core.context.history)
        await core.context.add_message("user", "WEB_MESSAGE")
        core._persist_sub_agent_reply(
            user_input="WEB_MESSAGE",
            reply="WEB_REPLY",
            user_id=user_id,
            source=source,
            emotion={},
            session_id=session_id,
            model_used="web-model",
            ctx=ctx,
        )
        return ProcessResult(reply="WEB_REPLY")

    core._dispatch_single_sub_agent = dispatch

    result = await core.dispatch_web_sub_agent(
        "xiaolang",
        "WEB_MESSAGE",
        session_id="web-session",
    )

    assert result.reply == "WEB_REPLY"
    assert observed["source"] == "web"
    assert observed["user_id"] == "web-context"
    assert observed["history_before"] == []
    web_token = observed["token"]
    assert web_token.user_id == "web-context"
    assert core.context.get_user_context_token() == web_token
    background_kwargs = core._bg_task_manager.run_background_tasks.call_args.kwargs
    assert background_kwargs["user_context_token"] == web_token

    await core.context.switch_user_context("qq_guest", address_term="朋友")
    assert core.context.history == [{"role": "user", "content": "GROUP_GUEST_HISTORY"}]
    assert "WEB" not in core.context._build_dynamic_prompt()


@pytest.mark.asyncio
async def test_web_direct_subagent_waits_for_group_context_lock() -> None:
    core = _web_dispatch_core()
    await core.context.switch_user_context("qq_guest", address_term="朋友")
    core.context.history = [{"role": "user", "content": "GROUP_ONLY"}]
    group_entered = asyncio.Event()
    release_group = asyncio.Event()
    web_dispatched = asyncio.Event()

    async def hold_group_context() -> None:
        async with core._context_lock:
            group_entered.set()
            await release_group.wait()
            assert core.context.get_user_context_token().user_id == "qq_guest"

    async def dispatch(*_args, **kwargs):
        ctx = kwargs["ctx"]
        assert core.context.get_user_context_token() == ctx.user_context_token
        web_dispatched.set()
        return ProcessResult(reply="WEB_REPLY")

    core._dispatch_single_sub_agent = dispatch
    group_task = asyncio.create_task(hold_group_context())
    await group_entered.wait()
    web_task = asyncio.create_task(core.dispatch_web_sub_agent(
        "xiaolang",
        "WEB_MESSAGE",
        session_id="web-session",
    ))
    await asyncio.sleep(0)
    assert not web_dispatched.is_set()

    release_group.set()
    await asyncio.gather(group_task, web_task)
    assert web_dispatched.is_set()
    assert core.context.get_user_context_token().user_id == "web-context"

    await core.context.switch_user_context("qq_guest", address_term="朋友")
    assert core.context.history == [{"role": "user", "content": "GROUP_ONLY"}]
