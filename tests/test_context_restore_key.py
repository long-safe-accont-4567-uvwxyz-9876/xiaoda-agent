"""Bug 1 (P0-1): QQ 会话恢复键与写库键不一致 → 突然失忆

根因：message_processor._init_and_restore_context 中
  _restore_id = user_openid or user_id   ← 优先用裸 openid
而写库键是 qq_{openid}（qq_bot_adapter.py:628），查询用裸 openid → DB 返回 0 行 → 失忆。

修复：_restore_id = user_id or user_openid（优先用带 qq_ 前缀的 user_id）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_context import AgentContext
from agent_core.message_processor import MessageProcessorMixin


def _make_processor(**overrides):
    """构造最小化 mixin 宿主对象，mock 掉所有外部依赖。"""
    proc = MagicMock()
    proc._tool_call_handler = MagicMock()
    proc._tool_call_handler._tool_repair = MagicMock()
    proc.security = MagicMock()
    proc.security.is_allowed = MagicMock(return_value=(True, ""))
    proc.context = MagicMock()
    proc.context.switch_user_context = AsyncMock()
    proc.context.restore_from_db = AsyncMock(return_value=True)
    proc.context.claim_user_context_resources = AsyncMock(return_value=True)
    proc.context.complete_user_context_resources = AsyncMock(return_value=True)
    proc.context.fail_user_context_resources = AsyncMock(return_value=True)
    proc.context.current_address_term = "爸爸"
    proc.db = MagicMock()  # truthy，触发 restore_from_db 分支
    for k, v in overrides.items():
        setattr(proc, k, v)
    return proc


@pytest.mark.asyncio
async def test_restore_id_prefers_user_id_with_qq_prefix():
    """_restore_id 应优先用带 qq_ 前缀的 user_id，与写库键一致。

    写库键：qq_bot_adapter.py:628  user_id = f"qq_{user_openid}"
    恢复键：应使用同一个 user_id，而非裸 openid。
    """
    proc = _make_processor()
    ctx = MagicMock()
    ctx.identity.is_owner = True

    user_openid = "ABCDEF123456"
    user_id = f"qq_{user_openid}"  # 与写库键一致

    await MessageProcessorMixin._init_and_restore_context(
        proc, ctx, "你好", user_id, "qq", None, user_openid, "session-1",
    )

    # switch_user_context 必须用带 qq_ 前缀的 user_id
    assert proc.context.switch_user_context.await_count == 1
    actual_restore_id = proc.context.switch_user_context.await_args.args[0]
    assert actual_restore_id == user_id, (
        f"恢复键应使用 user_id('{user_id}') 与写库键一致，"
        f"但实际用了 '{actual_restore_id}'（裸 openid）→ 会导致 DB 查询 0 行 → 失忆"
    )

    # restore_from_db 必须携带同一用户的完整 personal Scope。
    assert proc.context.restore_from_db.await_count == 1
    actual_scope = proc.context.restore_from_db.await_args.kwargs.get("scope")
    assert actual_scope.user_id == user_id
    assert actual_scope.boundary.value == "personal"


@pytest.mark.asyncio
async def test_restore_id_falls_back_to_openid_when_no_user_id():
    """当 user_id 为空时，回退到 user_openid（兼容无前缀场景）。"""
    proc = _make_processor()
    ctx = MagicMock()
    ctx.identity.is_owner = True

    user_openid = "ABCDEF123456"

    await MessageProcessorMixin._init_and_restore_context(
        proc, ctx, "你好", "", "qq", None, user_openid, "session-1",
    )

    assert proc.context.switch_user_context.await_count == 1
    actual_restore_id = proc.context.switch_user_context.await_args.args[0]
    assert actual_restore_id == user_openid


class _NoHistoryDB:
    async def get_conversations_readonly(self, **_kwargs):
        return []


class _FailingMemoryDB:
    async def get_recent_conversations(self, **_kwargs):
        raise RuntimeError("fallback unavailable")


class _FailingDB:
    def __init__(self):
        self.memory = _FailingMemoryDB()

    async def get_conversations_readonly(self, **_kwargs):
        raise RuntimeError("readonly unavailable")


class _PausedDB:
    def __init__(self, rows):
        self.rows = rows
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_conversations_readonly(self, **_kwargs):
        self.started.set()
        await self.release.wait()
        return self.rows


@pytest.mark.asyncio
async def test_private_group_a_group_b_private_history_and_restore_are_isolated(tmp_path):
    from db.database import DatabaseManager
    from memory.scope import Scope

    database = DatabaseManager(tmp_path / "context-scope.db")
    await database.init()
    private = Scope.personal("alice", "private-2")
    group_a = Scope.group("alice", "group-a")
    group_b = Scope.group("alice", "group-b")
    now = __import__("time").time()
    rows = (
        ("alice", "qq_c2c", "private-1", "PRIVATE_DB"),
        ("qq_group:audit-a", "qq_group", group_a.session_id, "GROUP_A_DB"),
        ("qq_group:audit-b", "qq_group", group_b.session_id, "GROUP_B_DB"),
    )
    for user_id, source, session_id, marker in rows:
        await database.insert_conversation_log(
            user_id=user_id,
            source=source,
            user_message=marker,
            assistant_reply=f"{marker}_REPLY",
            session_id=session_id,
        )
    await database._conn.execute(
        "UPDATE conversation_logs SET timestamp=?", (now,)
    )
    await database._conn.commit()

    context = AgentContext(system_prompt="base")
    private_token = await context.switch_user_context("alice")
    assert await context.restore_from_db(
        database, scope=private, user_token=private_token
    )
    context.memory_retrieval = None
    await context.add_message("user", "PRIVATE_LIVE")
    assert "PRIVATE_DB" in context._restored_summary
    assert "GROUP_A_DB" not in context._restored_summary

    group_a_token = await context.switch_user_context(group_a.session_id)
    assert await context.restore_from_db(
        database, scope=group_a, user_token=group_a_token
    )
    context.memory_retrieval = None
    assert context.history == []
    assert "GROUP_A_DB" in context._restored_summary
    assert "PRIVATE_DB" not in context._restored_summary
    await context.add_message("user", "GROUP_A_LIVE")

    group_b_token = await context.switch_user_context(group_b.session_id)
    assert await context.restore_from_db(
        database, scope=group_b, user_token=group_b_token
    )
    context.memory_retrieval = None
    assert context.history == []
    assert "GROUP_B_DB" in context._restored_summary
    assert "GROUP_A_DB" not in context._restored_summary

    private_token = await context.switch_user_context("alice")
    assert await context.restore_from_db(
        database, scope=private, user_token=private_token
    )
    context.memory_retrieval = None
    prompt = context._build_dynamic_prompt()
    assert context.history == [{"role": "user", "content": "PRIVATE_LIVE"}]
    assert "PRIVATE_DB" in context._restored_summary
    assert "GROUP_A_DB" not in prompt
    assert "GROUP_B_DB" not in prompt
    await database.close()


@pytest.mark.asyncio
async def test_switch_to_user_without_history_clears_all_alice_prompt_state():
    context = AgentContext(system_prompt="base")
    token = await context.switch_user_context("alice")
    context.history = [{"role": "assistant", "content": "ALICE_HISTORY"}]
    context._compressed_summary = "ALICE_COMPRESSED"
    context._restored_summary = "ALICE_RESTORED"
    context._pre_compressed_buffer = [{"role": "user", "content": "ALICE_BUFFER"}]
    context.user_portrait = "ALICE_PORTRAIT"
    context.notebook_focus = "ALICE_NOTEBOOK"
    context.pending_tasks = ["ALICE_TASK"]
    context.xiaoli_context = "ALICE_XIAOLI"
    context.memory_retrieval = [{"summary": "ALICE_MEMORY"}]
    context.emotion_hint = "ALICE_EMOTION"
    await context.record_failure(
        token,
        "ALICE_FAILURE",
        "ALICE_FAILURE_INPUT",
    )
    context._build_dynamic_prompt()

    await context.switch_user_context("bob")
    await context.restore_from_db(_NoHistoryDB(), user_id="bob")
    context._build_stable_content = lambda _user_input: "stable"
    messages = await context.build_messages("hello", source="qq_group")
    prompt = "\n".join(str(message.get("content", "")) for message in messages)

    assert context._restored_summary == ""
    assert context.history == []
    assert context._pre_compressed_buffer == []
    assert context.memory_retrieval is None
    assert context.emotion_hint == ""
    assert context.user_portrait is None
    assert context.notebook_focus is None
    assert context.pending_tasks is None
    assert context.xiaoli_context is None
    assert context.consume_failure() is None
    assert "ALICE_" not in prompt


@pytest.mark.asyncio
async def test_switch_alice_bob_alice_restores_complete_user_state():
    context = AgentContext(system_prompt="base")
    context.current_address_term = "Alice称谓"
    await context.switch_user_context("alice")
    context.history = [{"role": "assistant", "content": "ALICE_HISTORY"}]
    context._compressed_summary = "ALICE_COMPRESSED"
    context._compress_count = 2
    context._pre_compressed_buffer = [{"role": "user", "content": "ALICE_BUFFER"}]
    context._restored_summary = "ALICE_RESTORED"
    context.memory_retrieval = [{"summary": "ALICE_MEMORY", "metadata": {"owner": "alice"}}]
    context.emotion_hint = "ALICE_EMOTION"
    context.user_portrait = "ALICE_PORTRAIT"
    context.notebook_focus = "ALICE_NOTEBOOK"
    context.pending_tasks = ["ALICE_TASK"]
    context.xiaoli_context = "ALICE_XIAOLI"
    context._last_message_time = 123.0
    context._last_failure = {
        "type": "ALICE_FAILURE",
        "input_preview": "ALICE_FAILURE_INPUT",
        "timestamp": 456.0,
    }

    context.current_address_term = "Bob称谓"
    await context.switch_user_context("bob")
    context.history = [{"role": "assistant", "content": "BOB_HISTORY"}]
    context._restored_summary = "BOB_RESTORED"
    context.memory_retrieval = [{"summary": "BOB_MEMORY"}]

    context.current_address_term = "Alice称谓"
    await context.switch_user_context("alice")

    assert context._current_user_id == "alice"
    assert context.current_address_term == "Alice称谓"
    assert context.history == [{"role": "assistant", "content": "ALICE_HISTORY"}]
    assert context._compressed_summary == "ALICE_COMPRESSED"
    assert context._compress_count == 2
    assert context._pre_compressed_buffer == [{"role": "user", "content": "ALICE_BUFFER"}]
    assert context._restored_summary == "ALICE_RESTORED"
    assert context.memory_retrieval == [{
        "summary": "ALICE_MEMORY",
        "metadata": {"owner": "alice"},
    }]
    assert context.emotion_hint == "ALICE_EMOTION"
    assert context.user_portrait == "ALICE_PORTRAIT"
    assert context.notebook_focus == "ALICE_NOTEBOOK"
    assert context.pending_tasks == ["ALICE_TASK"]
    assert context.xiaoli_context == "ALICE_XIAOLI"
    assert context._last_message_time == 123.0
    assert context._last_failure == {
        "type": "ALICE_FAILURE",
        "input_preview": "ALICE_FAILURE_INPUT",
        "timestamp": 456.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("db", [_NoHistoryDB(), _FailingDB()], ids=["empty", "error"])
async def test_db_restore_empty_or_error_clears_stale_summary(db):
    context = AgentContext(system_prompt="base")
    await context.switch_user_context("bob")
    context._restored_summary = "STALE_PRIVATE_SUMMARY"
    assert "STALE_PRIVATE_SUMMARY" in context._build_dynamic_prompt()

    await context.restore_from_db(db, user_id="bob")

    assert context._restored_summary == ""
    assert "STALE_PRIVATE_SUMMARY" not in context._build_dynamic_prompt()


@pytest.mark.asyncio
async def test_memory_retrieval_is_isolated_and_restored_per_user():
    context = AgentContext(system_prompt="base")
    await context.switch_user_context("alice")
    alice_memory = [{"summary": "ALICE_MEMORY", "metadata": {"owner": "alice"}}]
    context.memory_retrieval = alice_memory

    await context.switch_user_context("bob")
    assert context.memory_retrieval is None
    context.memory_retrieval = [{"summary": "BOB_MEMORY"}]

    await context.switch_user_context("alice")
    assert context.memory_retrieval == alice_memory

    await context.switch_user_context("bob")
    assert context.memory_retrieval == [{"summary": "BOB_MEMORY"}]


@pytest.mark.asyncio
async def test_restore_started_for_alice_cannot_clear_bob_after_switch():
    context = AgentContext(system_prompt="base")
    await context.switch_user_context("alice", address_term="Alice称谓")
    db = _PausedDB([])

    restore = asyncio.create_task(context.restore_from_db(db, user_id="alice"))
    await db.started.wait()
    await context.switch_user_context("bob", address_term="Bob称谓")
    context._restored_summary = "BOB_RESTORED"
    context._build_dynamic_prompt()

    db.release.set()
    await restore

    assert context._current_user_id == "bob"
    assert context._restored_summary == "BOB_RESTORED"
    assert "BOB_RESTORED" in context._build_dynamic_prompt()


@pytest.mark.asyncio
async def test_restore_started_in_old_alice_epoch_cannot_write_after_aba_switch():
    context = AgentContext(system_prompt="base")
    await context.switch_user_context("alice", address_term="Alice称谓")
    db = _PausedDB([{
        "user_message": "OLD_ALICE_PRIVATE",
        "assistant_reply": "OLD_ALICE_REPLY",
        "timestamp": 0,
    }])

    restore = asyncio.create_task(context.restore_from_db(db, user_id="alice"))
    await db.started.wait()
    await context.switch_user_context("bob", address_term="Bob称谓")
    await context.switch_user_context("alice", address_term="Alice新称谓")
    context._restored_summary = "NEW_ALICE_RESTORED"

    db.release.set()
    await restore

    assert context._current_user_id == "alice"
    assert context.current_address_term == "Alice新称谓"
    assert context._restored_summary == "NEW_ALICE_RESTORED"
    assert "OLD_ALICE_PRIVATE" not in context._build_dynamic_prompt()


@pytest.mark.asyncio
async def test_switch_address_term_argument_overrides_cached_term():
    context = AgentContext(system_prompt="base")
    await context.switch_user_context("shared", address_term="旧称谓")
    await context.switch_user_context("other", address_term="朋友")

    await context.switch_user_context("shared", address_term="新身份称谓")

    assert context.current_address_term == "新身份称谓"
