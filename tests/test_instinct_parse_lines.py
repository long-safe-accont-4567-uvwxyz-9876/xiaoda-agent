"""extract_instincts F(42) 拆分契约测试。

原 extract_instincts 圈复杂度 F(42)，拆分后：
- extract_instincts 变编排（约 30 行）
- _call_llm_for_instincts：LLM 调用 + 降级
- _parse_instinct_lines（静态纯函数）：逐行解析 + 过滤 + 去重
- _apply_corrections / _insert_instinct_rows

契约：_parse_instinct_lines 的解析行为与原函数内联逻辑完全一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from instinct_manager import InstinctManager


def _parse(result, existing=()):
    """调用静态 _parse_instinct_lines，返回 (rows, skipped, corrections)。"""
    return InstinctManager._parse_instinct_lines(
        result, list(existing), "session_x", 12345.0,
    )


def test_parse_new_format():
    """NEW | content | confidence 格式正常解析。"""
    rows, skipped, corr = _parse("NEW | 用户喜欢在深夜写代码 | 0.9")
    assert len(rows) == 1
    assert rows[0][0] == "用户喜欢在深夜写代码"
    assert rows[0][1] == 0.9
    assert rows[0][2] == "session_x"
    assert skipped == 0
    assert corr == []


def test_parse_correct_format():
    """CORRECT | content | action 收集到 corrections，不插入。"""
    rows, skipped, corr = _parse("CORRECT | 用户喜欢被打断时继续说 | archive")
    assert rows == []
    assert corr == [("用户喜欢被打断时继续说", "archive")]


def test_parse_correct_default_demote():
    """CORRECT 的 action 非法时默认 demote。"""
    rows, skipped, corr = _parse("CORRECT | 某内容 | 非法动作")
    assert corr == [("某内容", "demote")]


def test_parse_old_format():
    """旧格式（无前缀）content | confidence 仍兼容。"""
    rows, skipped, corr = _parse("用户偏好中文对话 | 0.85")
    assert len(rows) == 1
    assert rows[0][0] == "用户偏好中文对话"
    assert rows[0][1] == 0.85


def test_low_confidence_filtered():
    """confidence < 0.5 被过滤。"""
    rows, _, _ = _parse("低价值模式 | 0.3")
    assert rows == []


def test_short_content_filtered():
    """content 长度 < 5 被过滤。"""
    rows, _, _ = _parse("NEW | 短 | 0.9")
    assert rows == []


def test_thinking_keywords_filtered():
    """LLM 思考过程特征词被过滤。"""
    rows, _, _ = _parse("NEW | 首先我需要分析这个对话 | 0.9")
    assert rows == []


def test_prompt_example_filtered():
    """prompt 示例片段被复制时过滤。"""
    rows, _, _ = _parse("NEW | 用户喜欢用中文交流 | 0.9")
    assert rows == []


def test_invalid_pattern_filtered():
    """正则匹配的模板化内容被过滤。"""
    rows, _, _ = _parse("NEW | 用户行为模式：喜欢X | 0.9")
    assert rows == []


def test_tool_call_line_skipped():
    """<tool_call> 行和 ``` 行被跳过。"""
    rows, _, _ = _parse("<tool_call>xxx</tool_call>\n```\nNEW | 用户喜欢在深夜写代码 | 0.8")
    assert len(rows) == 1
    assert rows[0][0] == "用户喜欢在深夜写代码"


def test_duplicate_skipped():
    """与已有 content 高度相似（text_ratio >= 75）时跳过。"""
    rows, skipped, _ = _parse(
        "用户喜欢亲密互动 | 0.8",
        existing=["用户偏好亲密互动"],  # 高度相似
    )
    assert rows == []
    assert skipped == 1


def test_genuinely_new_not_skipped():
    """与已有 content 不相似时正常插入。"""
    rows, skipped, _ = _parse(
        "用户喜欢亲密互动 | 0.8",
        existing=["用户重视承诺，厌恶言而无信"],  # 完全不同
    )
    assert len(rows) == 1
    assert skipped == 0
