"""rust_core 混合加速 PoC 等价性测试 — perf/rust-hybrid-poc

验证 Rust NodeIndex.direct_channel 与 spreading_activation 纯 Python 路径
（_compute_idf + _direct_channel）在真实与边界数据上逐位一致。
Rust 模块不可用时整组 skip，不影响 CI。
"""
from __future__ import annotations

import json
import math

import pytest

from memory.spreading_activation import SpreadingActivationEngine
from tests.test_spreading_activation import engine  # noqa: F401  复用 fixture

try:
    from memory import rust_hybrid
    _RC = rust_hybrid._try_import()
except Exception:  # noqa: BLE001
    _RC = None

pytestmark = pytest.mark.skipif(_RC is None, reason="rust_core 扩展未构建")


def _py_reference(alive_nodes, query_keys, query):
    """与 spreading_activation._compute_idf + _direct_channel 逐行对齐的参考实现。"""
    n = len(alive_nodes)
    df = {}
    for node in alive_nodes.values():
        try:
            nk = set(json.loads(node.get("keys", "[]")))
        except (json.JSONDecodeError, TypeError):
            nk = set()
        for k in query_keys & nk:
            df[k] = df.get(k, 0) + 1
    idf = {k: math.log(n / (1 + df.get(k, 0))) for k in query_keys}
    direct = {}
    q_lower = query.lower()
    for nid, node in alive_nodes.items():
        try:
            node_keys = set(json.loads(node.get("keys", "[]")))
        except (json.JSONDecodeError, TypeError):
            node_keys = set()
        w_bias = 0.35 + 0.65 * node.get("weight", 1.0)
        shared = query_keys & node_keys
        if shared:
            s = sum(idf.get(k, 0) for k in shared)
            direct[nid] = direct.get(nid, 0) + s * w_bias
        n_text = node.get("text", "").lower()
        substr = sum(1 for w in query_keys if len(w) >= 4 and w in n_text)
        reverse = sum(1 for k in node_keys if len(k) >= 4 and k in q_lower)
        if substr + reverse:
            direct[nid] = direct.get(nid, 0) + (substr + reverse) * 0.6 * w_bias
    return direct


def _make_nodes(specs):
    out = {}
    for i, (keys, text, weight) in enumerate(specs):
        out[f"n{i}"] = {"keys": json.dumps(keys, ensure_ascii=False),
                        "text": text, "weight": weight}
    return out


def _assert_equivalent(alive_nodes, query, query_keys):
    ref = _py_reference(alive_nodes, set(query_keys), query)
    idx = rust_hybrid.RustNodeIndex(alive_nodes)
    got = idx.direct_channel(query, list(query_keys))
    assert set(ref.keys()) == set(got.keys()), (
        f"键集合不一致: py_only={set(ref) - set(got)} rust_only={set(got) - set(ref)}")
    for k, v in ref.items():
        assert abs(v - got[k]) < 1e-9, f"节点 {k} 分差 {v} vs {got[k]}"


def test_rust_index_basic_equivalence():
    nodes = _make_nodes([
        (["纳西妲", "须弥", "原神"], "纳西妲是原神须弥的草神", 1.2),
        (["编程", "python"], "用户喜欢 Python 编程语言", 1.0),
        (["天气"], "今天天气晴朗", 0.8),
    ])
    _assert_equivalent(nodes, "纳西妲今天有什么安排", ["纳西妲", "今天", "安排"])


def test_rust_index_substring_both_directions():
    # 双向子串：query 关键词 ⊂ 节点文本；节点关键词 ⊂ query
    nodes = _make_nodes([
        (["编程语言"], "我最喜欢的编程语言是 rust 和 python", 1.0),
        (["记忆优化"], "扩散激活通道的记忆优化已完成", 1.5),
    ])
    _assert_equivalent(nodes, "聊聊编程语言的记忆优化",
                       ["编程语言", "记忆优化", "聊聊"])


def test_rust_index_malformed_keys_fallback():
    # keys 字段损坏时与 Python except 分支一致：按空集处理
    nodes = {
        "a": {"keys": "{broken json", "text": "文本甲", "weight": 1.0},
        "b": {"keys": "not even json", "text": "文本乙", "weight": 0.9},
        "c": {"keys": "[]", "text": "丙包含关键词编程", "weight": 1.1},
    }
    _assert_equivalent(nodes, "编程相关查询", ["编程", "相关", "查询"])


def test_rust_index_empty_and_missing_fields():
    nodes = {
        "x": {"keys": "[]", "text": "", "weight": 1.0},          # 空 text
        "y": {"keys": "[]"},                                      # 缺 text/weight
        "z": {},                                                  # 全缺
    }
    _assert_equivalent(nodes, "任意查询词", ["任意", "查询"])


def test_rust_index_unicode_and_case():
    nodes = _make_nodes([
        (["RUST", "Python"], "RUST 与 Python 混合架构 PYTHON 大小写", 1.0),
    ])
    # 子串匹配两侧均 lower()，大小写不敏感——与 Python 行为一致
    _assert_equivalent(nodes, "rust python RUST", ["rust", "python"])


@pytest.mark.asyncio
async def test_engine_recall_matches_python_when_rust_enabled(engine, monkeypatch):
    """开关打开时 _get_rust_index 路径与纯 Python 参考实现一致。"""
    now = "2026-07-10T12:00:00+08:00"
    await engine.db.insert_node(
        id="r1", text="纳西妲是草神",
        keys=json.dumps(["纳西妲", "草神"]), created=now,
        last_accessed=now, valid_from=now)
    await engine.db.insert_node(
        id="r2", text="用户喜欢编程",
        keys=json.dumps(["用户", "编程"]), created=now,
        last_accessed=now, valid_from=now)
    await engine.db.create_edge("r1", "r2")

    alive = await engine.db.get_alive_nodes()
    query = "纳西妲是谁"
    keys = set(engine.key_extractor.extract(query, is_query=True))
    ref = _py_reference(alive, keys, query)

    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_ENABLED", True)
    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_MIN_NODES", 1)
    idx = engine._get_rust_index(alive)
    assert idx is not None
    got = idx.direct_channel(query, list(keys))
    assert set(ref.keys()) == set(got.keys())
    for k, v in ref.items():
        assert abs(v - got[k]) < 1e-9


def test_should_use_rust_gate(monkeypatch):
    """开关关闭时 should_use_rust 恒 False（默认行为不变）。"""
    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_ENABLED", False)
    assert rust_hybrid.should_use_rust(10000) is False
    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_ENABLED", True)
    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_MIN_NODES", 500)
    assert rust_hybrid.should_use_rust(499) is False
    assert rust_hybrid.should_use_rust(500) is (_RC is not None)
