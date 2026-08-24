import time
from typing import Any

from db.profile_store import ProfileField, ProfileStore
from memory.profile_policy import ProfileCandidate, ProfilePolicy
from memory.scope import Scope
from memory.scope import current_scope as _current_scope
from tool_engine.tool_registry import ToolPermission, ToolResult, register_tool

_store: ProfileStore | None = None
_policy: ProfilePolicy | None = None


def bind(store: ProfileStore) -> None:
    global _store, _policy
    _store = store
    _policy = ProfilePolicy(store)


def current_scope() -> Scope:
    return _current_scope()


def _dependencies() -> tuple[ProfileStore, ProfilePolicy]:
    if _store is None or _policy is None:
        raise RuntimeError("ProfileStore is not bound")
    return _store, _policy


def _serialize(field: ProfileField) -> dict[str, Any]:
    return {
        "namespace": field.namespace,
        "field_key": field.field_key,
        "value": field.value,
        "value_type": field.value_type,
        "valid_from": field.valid_from,
        "valid_to": field.valid_to,
        "learned_at": field.learned_at,
        "expired_at": field.expired_at,
        "source_type": field.source_type,
        "source_id": field.source_id,
    }


@register_tool(
    name="profile_get",
    description="精确读取当前用户的一个结构化档案字段",
    schema={
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "field_key": {"type": "string"},
        },
        "required": ["namespace", "field_key"],
    },
    permission=ToolPermission.READ_ONLY,
    category="memory",
)
async def profile_get(namespace: str, field_key: str) -> ToolResult:
    store, _ = _dependencies()
    scope = current_scope()
    field = await store.get_current(
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        namespace=namespace,
        field_key=field_key,
    )
    return ToolResult.ok(_serialize(field) if field else None)


@register_tool(
    name="profile_set",
    description="提交当前用户的结构化档案候选，由确定性策略校验后写入",
    schema={
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "field_key": {"type": "string"},
            "value": {},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["namespace", "field_key", "value", "confidence"],
    },
    permission=ToolPermission.READ_WRITE,
    category="memory",
)
async def profile_set(
    namespace: str,
    field_key: str,
    value: Any,
    confidence: float,
) -> ToolResult:
    _, policy = _dependencies()
    scope = current_scope()
    if not scope.request_id:
        return ToolResult.fail("trusted request id is not bound")
    decision = await policy.apply(
        scope,
        ProfileCandidate(
            namespace=namespace,
            field_key=field_key,
            value=value,
            confidence=confidence,
            source_type="agent_tool",
            source_id=scope.request_id,
        ),
    )
    if decision.status != "accepted" or decision.field is None:
        return ToolResult.fail(decision.reason)
    return ToolResult.ok(_serialize(decision.field))


@register_tool(
    name="profile_history",
    description="读取当前用户一个结构化档案字段的历史版本",
    schema={
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "field_key": {"type": "string"},
        },
        "required": ["namespace", "field_key"],
    },
    permission=ToolPermission.READ_ONLY,
    category="memory",
)
async def profile_history(namespace: str, field_key: str) -> ToolResult:
    store, _ = _dependencies()
    scope = current_scope()
    fields = await store.get_history(
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        namespace=namespace,
        field_key=field_key,
    )
    return ToolResult.ok([_serialize(field) for field in fields])


@register_tool(
    name="profile_forget",
    description="撤销当前用户一个结构化档案字段的当前值，并保留审计历史",
    schema={
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "field_key": {"type": "string"},
        },
        "required": ["namespace", "field_key"],
    },
    permission=ToolPermission.READ_WRITE,
    category="memory",
)
async def profile_forget(namespace: str, field_key: str) -> ToolResult:
    store, _ = _dependencies()
    scope = current_scope()
    if not scope.request_id:
        return ToolResult.fail("trusted request id is not bound")
    forgotten_at = time.time()
    forgotten = await store.forget_with_event(
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        session_id=scope.session_id,
        namespace=namespace,
        field_key=field_key,
        source_type="agent_tool",
        source_id=scope.request_id,
        forgotten_at=forgotten_at,
    )
    if forgotten is None:
        return ToolResult.ok({"forgotten": False})
    return ToolResult.ok({"forgotten": True, "forgotten_at": forgotten_at})
