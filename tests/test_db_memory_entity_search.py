"""db_memory 实体搜索去重后的结构契约测试。"""
from __future__ import annotations

from db.db_memory import _entity_like_conditions, _rows_to_entity_results


def test_entity_like_conditions_builds_conditions_and_params():
    conditions, params = _entity_like_conditions(["小妲", "公园"])
    assert conditions == "entities LIKE ? OR entities LIKE ?"
    assert params == ['%"小妲"%', '%"公园"%']


def test_rows_to_entity_results_parses_entity_list():
    class _Row(dict):
        pass

    row = _Row({"id": 1, "entities": '["小妲","公园"]'})
    results = _rows_to_entity_results([row])
    assert results[0]["entity_list"] == ["小妲", "公园"]


def test_rows_to_entity_results_handles_invalid_json():
    row = {"id": 1, "entities": "{bad"}
    results = _rows_to_entity_results([row])
    assert results[0]["entity_list"] == []
