
import pytest

from memory.scope import Scope, bind_scope, reset_scope
from tools import memory_tool


class FakeMemoryDB:
    def __init__(self):
        self.insert_scope = None
        self.deleted_id = None
        self.hard_deleted_id = None

    async def insert_episodic_memory(self, **kwargs):
        self.insert_scope = kwargs["scope"]
        return 7

    async def delete_memory_with_vector(self, memory_id, vector_store=None):
        self.deleted_id = memory_id
    async def hard_delete_raw_for_user_request(self, memory_id, vector_store=None):
        self.hard_deleted_id = memory_id
        return True


class FakeMemoryManager:
    def __init__(self):
        self.memory = FakeMemoryDB()
        self.vec = None
        self.retrieve_scopes = []
        self.retrieve_kwargs = []

    async def retrieve_memories(self, query, **kwargs):
        self.retrieve_scopes.append(kwargs.get("scope"))
        self.retrieve_kwargs.append(kwargs)
        if query == "remove":
            return [{"id": 9, "summary": "remove me", "is_raw": False}]
        if query == "remove raw":
            return [{"id": 10, "summary": "raw", "is_raw": True}]
        return []


@pytest.mark.asyncio
async def test_memory_tools_use_bound_request_scope():
    manager = FakeMemoryManager()
    memory_tool.bind(manager)
    scope = Scope(user_id="alice", session_id="chat-1", agent_id="xiaoda")
    token = bind_scope(scope)
    try:
        remembered = await memory_tool.remember("important")
        recalled = await memory_tool.recall("anything")
        forgotten = await memory_tool.forget("remove")
    finally:
        reset_scope(token)

    assert remembered.success is True
    assert recalled.success is True
    assert forgotten.success is True
    assert manager.memory.insert_scope == scope
    assert manager.retrieve_scopes == [scope, scope]
    assert manager.retrieve_kwargs[0]["conv_user_id"] == "alice"
    assert manager.memory.deleted_id == 9


@pytest.mark.asyncio
async def test_forget_raw_uses_explicit_hard_delete_path():
    manager = FakeMemoryManager()
    memory_tool.bind(manager)
    scope = Scope(user_id="alice", session_id="chat-1", agent_id="xiaoda")
    token = bind_scope(scope)
    try:
        forgotten = await memory_tool.forget("remove raw")
    finally:
        reset_scope(token)

    assert forgotten.success is True
    assert manager.memory.hard_deleted_id == 10
    assert manager.memory.deleted_id is None
