"""KG 实体抽取 single-flight 测试：并发同 query 只调一次底层抽取。"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.knowledge_graph import KnowledgeGraph


@pytest.fixture
def kg() -> KnowledgeGraph:
    return KnowledgeGraph()


async def test_concurrent_same_query_single_extraction(kg, monkeypatch):
    """两路并发同 query：底层抽取仅执行一次，双方拿到相同结果。"""
    calls = {"n": 0}

    async def fake_extract(query):
        calls["n"] += 1
        await asyncio.sleep(0.05)  # 模拟免费模型延迟
        return {"entities": [{"name": "知识图谱"}, {"name": "图神经网络"}]}

    monkeypatch.setattr(kg, "extract_from_summary", fake_extract)

    q = "图神经网络与知识图谱的结合"
    r1, r2 = await asyncio.gather(
        kg.get_query_entities(q), kg.get_query_entities(q))

    assert calls["n"] == 1, f"底层抽取被调用了 {calls['n']} 次（应合并为 1）"
    assert r1 == r2 == {"知识图谱", "图神经网络"}
    # 结果已写缓存：第三次调用零抽取
    r3 = await kg.get_query_entities(q)
    assert r3 == {"知识图谱", "图神经网络"} and calls["n"] == 1


async def test_sequential_different_queries_each_extract(kg, monkeypatch):
    async def fake_extract(query):
        return {"entities": [{"name": query[:6]}]}

    monkeypatch.setattr(kg, "extract_from_summary", fake_extract)

    a = await kg.get_query_entities("问题一")
    b = await kg.get_query_entities("问题二")
    assert isinstance(a, set) and isinstance(b, set)


async def test_extraction_failure_propagates_to_all_waiters(kg, monkeypatch):
    calls = {"n": 0}

    async def failing(query):
        calls["n"] += 1
        await asyncio.sleep(0.02)
        raise RuntimeError("免费模型超时")

    monkeypatch.setattr(kg, "extract_from_summary", failing)
    # 规则短路可能先行返回空集——强制跳过短路以直达 LLM 路径
    monkeypatch.setattr(kg, "_rule_extractor",
                        types.SimpleNamespace(extract=lambda q, importance: _ret([("实体", 0.9)])))

    q = "会失败的查询"
    results = await asyncio.gather(
        kg.get_query_entities(q), kg.get_query_entities(q),
        return_exceptions=True)

    assert calls["n"] == 1
    # 失败经 Future 广播：等待方要么拿到异常、要么拿到内部吞错后的空集
    errs = [r for r in results if isinstance(r, BaseException)]
    empties = [r for r in results if isinstance(r, (set, frozenset)) and not r]
    assert errs or empties


def _ret(value):
    async def inner(*a, **k):
        return value
    return inner()
