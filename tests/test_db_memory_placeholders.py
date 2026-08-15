"""db_memory SQL IN 占位符 helper 的单元测试。"""
from __future__ import annotations

from db.db_memory import _sql_placeholders


def test_sql_placeholders_multiple():
    assert _sql_placeholders([1, 2, 3]) == "?,?,?"


def test_sql_placeholders_single():
    assert _sql_placeholders(["a"]) == "?"


def test_sql_placeholders_empty():
    assert _sql_placeholders([]) == ""
