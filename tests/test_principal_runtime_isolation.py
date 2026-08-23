from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent_core.message_processor import MessageProcessorMixin
from memory.memory_manager import MemoryManager
from memory.scope import Scope, bind_scope, reset_scope


async def test_main_llm_owner_flag_comes_from_resolved_principal():
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor._setup_main_emotion_and_memory = AsyncMock(return_value=({}, "neutral"))
    processor._build_main_messages = AsyncMock(return_value=([], None, []))
    processor._resolve_task_and_circuit = MagicMock(
        return_value=(None, "chat", 100, "closed", {})
    )
    processor._call_main_llm_with_verification = AsyncMock(return_value=("ok", []))
    processor._finalize_main_reply = AsyncMock(return_value=SimpleNamespace(reply="ok"))
    processor.security = MagicMock()
    ctx = SimpleNamespace(principal=SimpleNamespace(is_owner=True), is_master=True)

    await processor._run_main_process_path(
        ctx=ctx,
        user_input="hi",
        clean_input="hi",
        user_id="shared_context:shared",
        source="web",
        user_openid="owner",
        session_id="s1",
        status_callback=None,
        image_data=None,
        is_master=True,
        force_voice=False,
        trace={},
    )

    processor.security.is_owner.assert_not_called()
    assert processor._call_main_llm_with_verification.await_args.args[-1] is True


async def test_idle_encode_uses_bound_scope():
    manager = MemoryManager.__new__(MemoryManager)
    manager._pending_encode = True
    manager._last_message_time = 0
    manager._last_encode_time = 0
    manager._encode_generation = 1
    manager.IDLE_THRESHOLD = 0
    manager.ENCODE_COOLDOWN = 0
    manager.encode_memory = AsyncMock()
    scope = Scope(user_id="alice", session_id="s1", agent_id="xiaoda")
    token = bind_scope(scope)
    try:
        await manager.try_idle_encode({"exchanges": [{}, {}]}, force=True)
    finally:
        reset_scope(token)

    manager.encode_memory.assert_awaited_once_with(
        {"exchanges": [{}, {}]}, scope=scope
    )
