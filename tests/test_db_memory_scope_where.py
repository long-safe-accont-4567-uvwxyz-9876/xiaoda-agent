"""db_memory scope 过滤 helper 的单元测试。"""
from __future__ import annotations

from db.db_memory import _scope_where
from memory.scope import Scope


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
