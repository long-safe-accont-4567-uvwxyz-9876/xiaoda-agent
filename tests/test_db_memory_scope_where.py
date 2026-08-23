"""db_memory scope 过滤 helper 的单元测试。"""
from __future__ import annotations

from db.db_memory import _scope_where
from memory.scope import Scope, ScopeBoundary


def test_scope_where_without_is_raw():
    where, params = _scope_where(Scope(user_id="alice", agent_id="xiaoli"))
    assert where == " AND user_id = ? AND agent_id = ? AND session_id != 'archived'"
    assert params == ["alice", "xiaoli"]


def test_scope_where_with_is_raw():
    where, params = _scope_where(Scope(), is_raw=0)
    assert where.endswith(" AND is_raw = ?")
    assert params == ["default", "xiaoda", 0]


def test_scope_where_with_table_prefix_no_archived():
    where, params = _scope_where(
        Scope(user_id="a", agent_id="b"), is_raw=1,
        table="em", include_archived_filter=False,
    )
    assert where == " AND em.user_id = ? AND em.agent_id = ? AND em.is_raw = ?"
    assert params == ["a", "b", 1]


def test_private_scope_excludes_group_sessions_but_shares_private_sessions():
    scope = Scope.personal(
        user_id="alice", session_id="private-2", agent_id="xiaoda"
    )

    where, params = _scope_where(scope)

    assert scope.boundary is ScopeBoundary.PERSONAL
    assert "session_id NOT LIKE ?" in where
    assert params == ["alice", "xiaoda", "qq_group:%"]
    assert scope.matches_record({
        "user_id": "alice", "agent_id": "xiaoda", "session_id": "private-1"
    })
    assert not scope.matches_record({
        "user_id": "alice", "agent_id": "xiaoda", "session_id": "qq_group:group-a"
    })


def test_group_scope_requires_exact_conversation_session():
    scope = Scope.group(user_id="alice", group_id="group-a", agent_id="xiaoda")

    where, params = _scope_where(scope)

    assert scope.boundary is ScopeBoundary.CONVERSATION
    assert "session_id = ?" in where
    assert params == ["alice", "xiaoda", "qq_group:group-a"]
    assert scope.matches_record({
        "user_id": "alice", "agent_id": "xiaoda", "session_id": "qq_group:group-a"
    })
    assert not scope.matches_record({
        "user_id": "alice", "agent_id": "xiaoda", "session_id": "qq_group:group-b"
    })
