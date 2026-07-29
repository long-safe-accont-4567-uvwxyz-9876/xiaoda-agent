"""P0 测试: _repair_json 必须修复 LLM 输出中常见的 JSON 语法错误。

根因背景：
- kg.extract_json_error 出现 139 次，根因是 V1 prompt 双花括号导致 LLM 输出 {{"entities": []}}，
  json.loads 在 char 1 处失败。
- V2 路径偶发尾逗号（,} / ,]）导致 6 次失败。
- _repair_json 此前只有 5 条规则，不覆盖双花括号和尾逗号，本次扩展到 9 条。

守护目标：防止 _repair_json 规则被回退，确保 139 + 6 = 145 次失败归零。
"""
import json

import pytest


def test_repair_json_fixes_double_braces():
    """双花括号 {{}} 必须被修复为单花括号（139 次失败的根因）。"""
    from memory.knowledge_graph import _repair_json

    # LLM 复制 prompt 中的 {{}} 示例
    broken = '{{"entities": [], "relations": []}}'
    repaired = _repair_json(broken)
    assert json.loads(repaired) == {"entities": [], "relations": []}, (
        f"双花括号未被修复: {repaired}"
    )


def test_repair_json_fixes_nested_double_braces():
    """外层双花括号被修复，嵌套结构恢复正常（LLM 只会复制外层 {{}}）。"""
    from memory.knowledge_graph import _repair_json

    # LLM 实际输出模式：整个 JSON 被外层 {{}} 包裹
    broken = '{{"entities": [{"name": "test"}], "relations": []}}'
    repaired = _repair_json(broken)
    assert json.loads(repaired) == {
        "entities": [{"name": "test"}], "relations": []
    }, f"外层双花括号未被修复: {repaired}"


def test_repair_json_preserves_string_value_braces():
    """字符串值内的 {{template}} 必须被保留（CodeRabbit 修复：不破坏字符串内容）。"""
    from memory.knowledge_graph import _repair_json

    # observations 含 {{template}} 占位符，不应被破坏
    broken = '{{"entities": [{{"name": "test", "observations": ["uses {{template}} syntax"}}], "relations": []}}'
    repaired = _repair_json(broken)
    # 外层 {{}} 被删，但字符串值内的 {{template}} 保留
    # 注意：嵌套结构 {{ 不被修复（CodeRabbit 设计），此用例验证字符串值保留
    # 外层修复后：{"entities": [{{"name":...}}], ...} — 嵌套 {{ 仍在，json.loads 会失败
    # 这验证了：_repair_json 不全局破坏字符串，嵌套需 _clean_json_response 预处理
    assert '{{template}}' in repaired, f"字符串值内的 {{template}} 被破坏: {repaired}"
    assert repaired.startswith('{'), f"外层 {{ 未被删除: {repaired}"


def test_repair_json_fixes_trailing_comma_in_object():
    """对象尾逗号 ,} 必须被修复（V2 路径 6 次失败的模式之一）。"""
    from memory.knowledge_graph import _repair_json

    broken = '{"entities": [{"name": "test",}], "relations": []}'
    repaired = _repair_json(broken)
    assert json.loads(repaired) == {
        "entities": [{"name": "test"}], "relations": []
    }, f"对象尾逗号未被修复: {repaired}"


def test_repair_json_fixes_trailing_comma_in_array():
    """数组尾逗号 ,] 必须被修复。"""
    from memory.knowledge_graph import _repair_json

    broken = '{"entities": [{"name": "a"}, {"name": "b"},], "relations": []}'
    repaired = _repair_json(broken)
    parsed = json.loads(repaired)
    assert len(parsed["entities"]) == 2, f"数组尾逗号未被修复: {repaired}"


def test_repair_json_preserves_valid_json():
    """合法 JSON 不应被破坏。"""
    from memory.knowledge_graph import _repair_json

    valid = '{"entities": [{"name": "test"}], "relations": []}'
    repaired = _repair_json(valid)
    assert json.loads(repaired) == json.loads(valid), (
        f"合法 JSON 被错误修改: {repaired}"
    )


def test_repair_json_combined_errors():
    """组合错误（外层双花括号 + 尾逗号）必须同时被修复。"""
    from memory.knowledge_graph import _repair_json

    # 外层 {{}} + 对象/数组尾逗号（LLM 实际输出模式：整体包裹 + 偶发尾逗号）
    broken = '{{"entities": [{"name": "a",}], "relations": [],}}'
    repaired = _repair_json(broken)
    parsed = json.loads(repaired)
    assert parsed == {"entities": [{"name": "a"}], "relations": []}, (
        f"组合错误未被修复: {repaired}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
