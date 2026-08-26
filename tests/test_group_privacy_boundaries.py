from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core._shared import RequestContext, _current_request_ctx
from agent_core.message_processor import MessageProcessorMixin
from agent_core.mixins.main_path import MainPathMixin
from agent_core.sub_agent_manager import SubAgentManagerMixin
from core.background_tasks import (
    BackgroundTaskManager,
    reset_current_request_context,
    set_current_request_context,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_owner", "expected_calls"), [(True, 1), (False, 0)])
async def test_qq_group_xp_and_profile_is_owner_only(is_owner: bool, expected_calls: int) -> None:
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor._system_context = ""
    processor._parse_mode_markers = lambda value: value
    processor._init_and_restore_context = AsyncMock(
        return_value=(MagicMock(), "session", True, "")
    )
    processor._spawn_xp_and_profile = MagicMock()
    processor.slash_handler = MagicMock()
    processor.slash_handler.is_slash_command.return_value = True
    processor.slash_handler.handle = AsyncMock(return_value="done")
    ctx = RequestContext(is_master=is_owner)

    await processor._process_impl(
        ctx, "当前消息", "qq_actor", "qq_group", "member-openid", "",
        None, None, is_master=is_owner,
    )

    assert processor._spawn_xp_and_profile.call_count == expected_calls


class _MainPathHarness(MainPathMixin):
    def __init__(self) -> None:
        self.context = MagicMock()
        self.context.add_message = AsyncMock()
        self.router = MagicMock()
        self.router.pop_reasoning_content.return_value = ""
        self.router.get_current_chat_model.return_value = {"model_id": "model"}
        self.router.flush_costs = AsyncMock()
        self.sticker_manager = MagicMock()
        self.sticker_manager.strip_emotion_tag.side_effect = lambda value: value
        self._bg_task_manager = MagicMock()
        self._bg_task_manager.learning_manager = None
        self._extract_media_from_tool_results = AsyncMock(return_value=([], None, "答复"))
        self._extract_fabricated_images_from_reply = AsyncMock(return_value=([], "答复"))
        self._apply_persona_critic = MagicMock()
        self._hook_engine = MagicMock()
        self._hook_engine.fire_post_response = AsyncMock()
        self.security = MagicMock()
        self.security.check_output_privacy.return_value = (True, "答复", "")
        self.get_sticker_info = MagicMock(return_value=("答复", None))
        self._clean_reply_full = MagicMock(return_value="答复")
        self._build_voice_result = AsyncMock(return_value=(None, False, ""))


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_owner", "full_calls", "log_calls"), [(True, 1, 0), (False, 0, 1)])
async def test_group_owner_runs_full_background_non_owner_is_log_only(
    is_owner: bool, full_calls: int, log_calls: int,
) -> None:
    harness = _MainPathHarness()
    ctx = RequestContext(is_master=is_owner)
    ctx.principal = SimpleNamespace(is_owner=is_owner)
    token = _current_request_ctx.set(ctx)
    try:
        await harness._finalize_main_reply(
            "答复", [], "当前消息", "qq_actor", "qq_group", {}, "neutral",
            ctx, "member-openid", is_owner, None, False, MagicMock(), "opaque-session",
        )
    finally:
        _current_request_ctx.reset(token)

    assert harness._bg_task_manager.run_background_tasks.call_count == full_calls
    assert harness._bg_task_manager.log_conversation_only.call_count == log_calls
    if not is_owner:
        harness.context.add_message.assert_not_awaited()
        args = harness._bg_task_manager.log_conversation_only.call_args.args
        assert args[:4] == ("当前消息", "答复", "qq_actor", "qq_group")


class _TransactionDB:
    def __init__(self) -> None:
        self.insert_conversation_log = AsyncMock()
        self.update_session = AsyncMock()

    @asynccontextmanager
    async def write_transaction(self):
        yield


async def _wait_for_owned_tasks(manager: BackgroundTaskManager) -> None:
    for _ in range(20):
        tasks = manager.get_owned_tasks()
        if not tasks:
            await asyncio.sleep(0)
            if not manager.get_owned_tasks():
                return
            continue
        await asyncio.gather(*tasks)
    raise AssertionError("background tasks did not drain")


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_owner", "expected_encode_calls"), [(True, 1), (False, 0)])
async def test_main_path_group_audit_uses_opaque_identity_and_owner_still_encodes(
    is_owner: bool, expected_encode_calls: int,
) -> None:
    db = _TransactionDB()
    context = MagicMock(history=[{"role": "user", "content": "历史"}] * 6)
    context.add_message = AsyncMock()
    context.get_user_context_token.return_value = None
    context.flush_pre_compressed_buffer = AsyncMock(return_value=[])
    context.get_last_n.return_value = [
        {"role": "user", "content": "主人私有上下文"},
        {"role": "assistant", "content": "历史答复"},
    ]
    memory = MagicMock()
    memory.try_idle_encode = AsyncMock()
    manager = BackgroundTaskManager(db=db, context=context, memory=memory)
    manager._run_manager_tasks = AsyncMock()
    manager._run_scheduled_tasks = AsyncMock()
    harness = _MainPathHarness()
    harness.context = context
    harness._bg_task_manager = manager
    ctx = RequestContext(is_master=is_owner)
    ctx.principal = SimpleNamespace(is_owner=is_owner)
    ctx.group_context_metadata = {
        "chat_type": "qq_group",
        "group_key": "opaque-group-hash",
        "actor_alias": "成员A",
        "is_owner": is_owner,
        "message_id": "message-1",
    }

    token = _current_request_ctx.set(ctx)
    try:
        await harness._finalize_main_reply(
            "答复", [], "当前消息", "qq_member-openid-secret", "qq_group", {},
            "neutral", ctx, "member-openid-secret", is_owner, None, False,
            MagicMock(), "qq_group:group-openid-real",
        )
        await _wait_for_owned_tasks(manager)
    finally:
        _current_request_ctx.reset(token)

    inserted = db.insert_conversation_log.await_args.kwargs
    assert inserted["user_id"] == "qq_group:opaque-group-hash"
    assert inserted["session_id"] == "qq_group:group-openid-real"
    assert "member" not in inserted["user_id"]
    assert "openid" not in inserted["user_id"]
    assert "member" not in inserted["session_id"]
    assert json.loads(inserted["request_context_json"]) == ctx.group_context_metadata
    assert db.update_session.await_count == 0
    assert memory.try_idle_encode.await_count == expected_encode_calls


@pytest.mark.asyncio
async def test_log_only_audit_writes_metadata_without_encoding_or_manager_tasks() -> None:
    db = _TransactionDB()
    context = MagicMock(history=[{"role": "system", "content": "群buffer秘密"}] * 6)
    context.get_last_n = MagicMock(return_value=[{"role": "user", "content": "群buffer秘密"}])
    memory = MagicMock()
    memory.try_idle_encode = AsyncMock()
    manager = BackgroundTaskManager(db=db, context=context, memory=memory)
    manager._run_manager_tasks = AsyncMock()
    manager._run_scheduled_tasks = AsyncMock()
    metadata = {
        "chat_type": "qq_group",
        "group_key": "opaque-group-hash",
        "actor_alias": "成员A",
        "is_owner": False,
        "message_id": "message-1",
    }
    token = set_current_request_context(metadata)
    try:
        await manager._run_persistence_tasks(
            "当前消息", "答复", "qq_actor", "qq_group", {}, "opaque-session",
            log_only=True,
        )
    finally:
        reset_current_request_context(token)

    db.insert_conversation_log.assert_awaited_once()
    db.update_session.assert_not_awaited()
    memory.try_idle_encode.assert_not_awaited()
    manager._run_manager_tasks.assert_not_awaited()
    raw = db.insert_conversation_log.await_args.kwargs["request_context_json"]
    assert json.loads(raw) == metadata
    assert "member_openid" not in raw
    assert "群buffer秘密" not in raw


class _SubAgentPrivacyHarness(SubAgentManagerMixin):
    def __init__(self) -> None:
        self.dispatcher = MagicMock()
        self.dispatcher.get_agent.return_value = SimpleNamespace(
            available=True,
            config=SimpleNamespace(display_name="小狼"),
        )
        self.dispatcher.dispatch = AsyncMock(return_value="子代理答复")
        self.context = MagicMock()
        self.context.current_address_term = "朋友"
        self.context.get_last_n.return_value = []
        self.context.compressed_summary = ""
        self.context.user_portrait = None
        self.context.add_message = AsyncMock()
        self.context.belief_router = None
        self._bg_task_manager = MagicMock()
        self.router = MagicMock()
        self.router.get_current_chat_model.return_value = {"model_id": "model"}
        self.security = MagicMock()
        self.security.is_owner.return_value = False
        self.security.check_output_privacy.return_value = (True, "子代理答复", "")
        self._voice_mode = False
        self.tts = MagicMock(available=False)
        self.get_sticker_manager = MagicMock(return_value=MagicMock(available=False))
        self.sticker_manager = MagicMock()
        self.sticker_manager.strip_emotion_tag.side_effect = lambda value: value
        self.get_sticker_info = MagicMock(return_value=("并行答复", None))
        self._finalize_reply = MagicMock(side_effect=lambda value, **_kwargs: value)
        self._clean_reply = MagicMock(side_effect=lambda value: value)


@pytest.mark.asyncio
async def test_single_subagent_group_guest_is_log_only() -> None:
    harness = _SubAgentPrivacyHarness()
    ctx = RequestContext(user_id="qq_guest", is_master=False)
    ctx.principal = SimpleNamespace(is_owner=False)
    ctx.user_context_token = "guest-token"
    ctx.group_context_metadata = {"group_key": "opaque", "is_owner": False}

    await harness._dispatch_single_sub_agent(
        "xiaolang",
        "访客问题",
        "qq_guest",
        "qq_group",
        "group-session",
        MagicMock(),
        ctx=ctx,
    )

    harness._bg_task_manager.run_background_tasks.assert_not_called()
    harness._bg_task_manager.log_conversation_only.assert_called_once()


@pytest.mark.asyncio
async def test_parallel_subagent_group_guest_is_log_only() -> None:
    harness = _SubAgentPrivacyHarness()
    ctx = RequestContext(user_id="qq_guest", is_master=False)
    ctx.principal = SimpleNamespace(is_owner=False)
    ctx.user_context_token = "guest-token"
    ctx.group_context_metadata = {"group_key": "opaque", "is_owner": False}

    await harness._finalize_parallel_reply(
        "并行答复",
        "访客问题",
        "qq_guest",
        "qq_group",
        "group-session",
        False,
        ctx,
        intermediate=[],
        model_used="model",
    )

    harness._bg_task_manager.run_background_tasks.assert_not_called()
    harness._bg_task_manager.log_conversation_only.assert_called_once()
