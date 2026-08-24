"""query_transform 提示词常量化后的字节快照与 override 接管契约。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.query_transform import (
    CLASSIFY_PROMPT,
    EXPAND_PROMPT,
    HYDE_PROMPT,
    REWRITE_PROMPT,
)
from web.prompt_profiles import profile_by_id


def test_rewrite_snapshot_context_truncation():
    long_context = "前" * 300 + "尾部关键信息"
    prompt = (
        REWRITE_PROMPT
        .replace("{original_query}", "那个方案怎么样{}")
        .replace("{context_block}", long_context[-200:] if long_context else "无")
    )
    assert "那个方案怎么样{}" in prompt
    expected_context = long_context[-200:]
    assert f"对话上下文: {expected_context}" in prompt
    assert "前" * 195 not in prompt


def test_rewrite_empty_context_uses_wu():
    prompt = (
        REWRITE_PROMPT
        .replace("{original_query}", "测试")
        .replace("{context_block}", "无")
    )
    assert "对话上下文: 无" in prompt


def test_expand_snapshot_contains_n():
    prompt = EXPAND_PROMPT.replace("{n}", "3").replace("{query}", "q{}")
    assert "生成 3 个不同视角" in prompt
    assert "q{}" in prompt


def test_hyde_and_classify_snapshots():
    hyde = HYDE_PROMPT.replace("{query}", "Q1")
    assert hyde.startswith("请根据以下问题")
    assert hyde.endswith("问题: Q1\n")
    classify = CLASSIFY_PROMPT.replace("{query}", "昨天吃了什么")
    assert classify == (
        "请分类以下查询的意图类型（temporal/factual/chat/multi-hop），"
        "只输出类型名称：\n查询: 昨天吃了什么"
    )


def test_four_query_profiles_are_production_bound():
    for pid, expected_ref in [
        ("query.rewrite", "REWRITE_PROMPT"),
        ("query.expand", "EXPAND_PROMPT"),
        ("query.hyde", "HYDE_PROMPT"),
        ("query.classify", "CLASSIFY_PROMPT"),
    ]:
        profile = profile_by_id(pid)
        assert profile is not None and profile.status == "production"
        assert any(expected_ref in ref for ref in profile.template_refs), pid
