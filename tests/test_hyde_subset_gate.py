"""HyDE 子集门控契约测试：exact 类查询跳过假设文档，语义类保留。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import config as cfg_module
from memory.query_transform import QueryTransformer


def _transformer():
    return QueryTransformer(router=None, api_key="")


@pytest.mark.parametrize("query", [
    "我的证件号码是多少",
    "/v1/orders 接口报 ORDER_409_CONFLICT 是什么意思",
    "帮我把 FastAPI 的 timeout 改成 30s",
    "周三下午3点那个会改到几点了",
    "13800138000 是谁的号码",
    "我上次说自己对青霉素过敏是哪一年查出来的",
])
def test_exact_shaped_queries_are_excluded(query):
    assert _transformer().should_use_hyde(query) is False


@pytest.mark.parametrize("query", [
    "我上次去杭州玩得怎么样",
    "我大学室友后来在哪座城市做什么工作",
    "用户喜欢什么口味的菜",
    "那个项目后来进展如何",
])
def test_semantic_queries_pass_gate(query):
    assert _transformer().should_use_hyde(query) is True


class _FakeVec:
    def __init__(self):
        self.hyde_calls = 0
        self.plain_calls = 0

    async def search_with_hyde(self, query, hyde_doc=None, alpha=0.4,
                               k=5, candidate_ids=None):
        self.hyde_calls += 1
        return []

    async def search(self, query, top_k=5, candidate_ids=None,
                     deterministic=True, query_vec=None):
        self.plain_calls += 1
        return []


class _FakeTransformer:
    available = True

    def __init__(self, gate_result):
        self._gate_result = gate_result
        self.gate_calls: list[str] = []

    def should_use_hyde(self, query: str) -> bool:
        self.gate_calls.append(query)
        return self._gate_result

    async def generate_hyde_document(self, query, context=""):
        return f"hyde-doc for {query}"


def _make_host(vec: _FakeVec, transformer, monkeypatch, subset_mode: str):
    from memory.retrieval.channels import RecallChannelMixin

    mm = SimpleNamespace(
        vec=vec,
        _query_transformer=transformer,
    )

    class Host(RecallChannelMixin):
        pass

    host = Host()
    host._mm = mm

    async def fake_pages(scope, candidate_ids):
        yield None

    monkeypatch.setattr(host, "_iter_visible_candidate_pages", fake_pages)
    monkeypatch.setattr(cfg_module, "HYDE_ENABLED", True)
    monkeypatch.setattr(cfg_module, "HYDE_SUBSET_MODE", subset_mode)
    return host


@pytest.mark.asyncio
async def test_non_exact_mode_skips_hyde_for_exact_query(tmp_path, monkeypatch):
    vec = _FakeVec()
    transformer = _FakeTransformer(gate_result=False)
    host = _make_host(vec, transformer, monkeypatch, "non_exact")

    await host._hybrid_vec_search("证件号码 110105199001011234", k=5)

    assert vec.hyde_calls == 0 and vec.plain_calls == 1
    assert transformer.gate_calls == ["证件号码 110105199001011234"]


@pytest.mark.asyncio
async def test_non_exact_mode_keeps_hyde_for_semantic_query(tmp_path, monkeypatch):
    vec = _FakeVec()
    transformer = _FakeTransformer(gate_result=True)
    host = _make_host(vec, transformer, monkeypatch, "non_exact")

    await host._hybrid_vec_search("上次去杭州的旅行", k=5)

    assert vec.hyde_calls == 1 and vec.plain_calls == 0


@pytest.mark.asyncio
async def test_off_mode_preserves_legacy_behavior(tmp_path, monkeypatch):
    vec = _FakeVec()
    transformer = _FakeTransformer(gate_result=False)
    host = _make_host(vec, transformer, monkeypatch, "off")

    await host._hybrid_vec_search("证件号码", k=5)

    assert vec.hyde_calls == 1
