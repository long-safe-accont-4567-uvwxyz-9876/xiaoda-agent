"""db_memory FTS 行解析 helper 的单元测试。"""
from __future__ import annotations

from db.db_memory import _rows_to_fts_results


def test_rows_to_fts_results_negates_score():
    row = {"id": 1, "score": -3.5}
    results = _rows_to_fts_results([row])
    assert results[0]["score"] == 3.5


def test_rows_to_fts_results_missing_score():
    row = {"id": 2}
    results = _rows_to_fts_results([row])
    assert results[0]["score"] == 0
