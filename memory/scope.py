"""Scope 三级隔离 — user_id/session_id/agent_id 作用域控制。

用于 mem0 SPEC 优化的记忆隔离：
- user_id: 用户标识（默认 'default'，单用户桌面应用）
- session_id: 会话标识（复用已有字段，会话级隔离）
- agent_id: Agent 标识（xiaoda/xiaoli/xiaolian/xiaoke）
"""
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum


class ScopeBoundary(str, Enum):
    PERSONAL = "personal"
    CONVERSATION = "conversation"


@dataclass(frozen=True)
class Scope:
    """记忆隔离的三级 scope。

    默认值对应单用户桌面应用场景：
    - user_id='default': 单用户
    - session_id='user': 默认会话
    - agent_id='xiaoda': 默认 agent
    """
    user_id: str = "default"
    session_id: str = "user"
    agent_id: str = "xiaoda"
    request_id: str = ""
    _boundary: ScopeBoundary | None = field(default=None, repr=False, compare=False)

    @classmethod
    def personal(
        cls,
        user_id: str = "default",
        session_id: str = "user",
        agent_id: str = "xiaoda",
        request_id: str = "",
    ) -> "Scope":
        """Build a personal scope that shares memory across private sessions."""
        return cls(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            request_id=request_id,
            _boundary=ScopeBoundary.PERSONAL,
        )

    @classmethod
    def group(
        cls,
        user_id: str,
        group_id: str,
        agent_id: str = "xiaoda",
        request_id: str = "",
    ) -> "Scope":
        """Build a conversation-bound QQ group scope."""
        session_id = (
            group_id
            if group_id.startswith("qq_group:")
            else f"qq_group:{group_id}"
        )
        return cls(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            request_id=request_id,
            _boundary=ScopeBoundary.CONVERSATION,
        )

    @property
    def boundary(self) -> ScopeBoundary:
        if self._boundary is not None:
            return self._boundary
        if self.session_id.startswith("qq_group:"):
            return ScopeBoundary.CONVERSATION
        return ScopeBoundary.PERSONAL

    def matches_record(self, record: dict) -> bool:
        if (
            record.get("user_id") != self.user_id
            or record.get("agent_id") != self.agent_id
        ):
            return False
        record_session = str(record.get("session_id") or "")
        if record_session == "archived":
            return False
        if self.boundary is ScopeBoundary.CONVERSATION:
            return record_session == self.session_id
        return not record_session.startswith("qq_group:")

    def kg_partition_key(self) -> str:
        """返回 KG v2 分区键；私聊跨会话共享，QQ群按群会话隔离。"""
        base = f"{self.user_id}::{self.agent_id}"
        if self.session_id.startswith("qq_group:"):
            return f"{base}::{self.session_id}"
        return base

    def cache_namespace(self) -> str:
        """Return the scope-only cache namespace, excluding cache epochs."""
        return f"{self.kg_partition_key()}::{self.user_id}"

    def to_sql_filter(self, table: str = "episodic_memories") -> str:
        """Generate the complete literal privacy predicate for diagnostics."""
        user_id = self.user_id.replace("'", "''")
        agent_id = self.agent_id.replace("'", "''")
        where = (
            f"{table}.user_id = '{user_id}' "
            f"AND {table}.agent_id = '{agent_id}'"
        )
        if self.boundary is ScopeBoundary.CONVERSATION:
            session_id = self.session_id.replace("'", "''")
            where += f" AND {table}.session_id = '{session_id}'"
        else:
            where += (
                f" AND COALESCE({table}.session_id, '') NOT LIKE 'qq_group:%'"
                f" AND COALESCE({table}.session_id, '') != 'archived'"
            )
        return where

    def to_sql_params(self) -> list[str]:
        """Return parameters for the complete privacy-boundary predicate."""
        if self.boundary is ScopeBoundary.CONVERSATION:
            return [self.user_id, self.agent_id, self.session_id]
        return [self.user_id, self.agent_id, "qq_group:%"]

    def to_sql_filter_parametrized(self, table: str = "episodic_memories") -> tuple[str, list[str]]:
        """Generate the complete parameterized privacy-boundary predicate."""
        prefix = f"{table}." if table else ""
        where = f"{prefix}user_id = ? AND {prefix}agent_id = ?"
        params = [self.user_id, self.agent_id]
        if self.boundary is ScopeBoundary.CONVERSATION:
            where += f" AND {prefix}session_id = ?"
            params.append(self.session_id)
        else:
            where += f" AND COALESCE({prefix}session_id, '') NOT LIKE ?"
            where += f" AND COALESCE({prefix}session_id, '') != 'archived'"
            params.append("qq_group:%")
        return where, params


_current_scope: ContextVar[Scope | None] = ContextVar("memory_scope", default=None)


def bind_scope(scope: Scope) -> Token:
    return _current_scope.set(scope)


def reset_scope(token: Token) -> None:
    _current_scope.reset(token)


def current_scope() -> Scope:
    scope = _current_scope.get()
    if scope is None:
        raise RuntimeError("memory request scope is not bound")
    return scope


def current_scope_or_default() -> Scope:
    return _current_scope.get() or Scope()
