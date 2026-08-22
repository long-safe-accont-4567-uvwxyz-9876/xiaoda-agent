"""rust_core 混合加速 PoC 等价性测试 — perf/rust-hybrid-poc

验证 Rust NodeIndex.direct_channel 与 spreading_activation 纯 Python 路径
（_compute_idf + _direct_channel）在真实与边界数据上逐位一致。
Rust 模块不可用时整组 skip，不影响 CI。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

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


# ── 扩散激活图游走（spreading_channel）等价性 ──────────


def _py_spreading(direct, alive_nodes, graph, radius=3, decay=0.5,
                  threshold=0.05):
    """与 spreading_activation._spreading_channel 逐行对齐的参考实现。"""
    spread = defaultdict(float)
    wave = dict(direct)
    for hop in range(radius + 1):
        nxt = defaultdict(float)
        for nid, act in wave.items():
            spread[nid] += act
            if hop < radius and act > threshold:
                for nb, ew in graph.get(nid, {}).items():
                    if nb not in alive_nodes:
                        continue
                    nxt[nb] += act * decay * ew / (hop + 1)
        wave = nxt
        if not wave:
            break
    return dict(spread)


def test_rust_spreading_channel_equivalence():
    """图游走逐值一致：种子累积、衰减/跳数除法、阈值剪枝、alive 过滤。"""
    alive = {
        "a": {"keys": "[]", "text": "A", "weight": 1.0},
        "b": {"keys": "[]", "text": "B", "weight": 1.0},
        "c": {"keys": "[]", "text": "C", "weight": 1.0},
        "d": {"keys": "[]", "text": "D", "weight": 1.0},  # 不在图中（孤立）
    }
    graph = {
        "a": {"b": 0.8, "c": 0.4},
        "b": {"c": 0.9, "dead": 5.0},   # dead 不在 alive → 不中继
        "c": {"a": 0.2},
    }
    seeds = {"a": 1.0, "ghost": 0.8}    # ghost 不在节点集

    ref = _py_spreading(seeds, alive, graph)
    idx = rust_hybrid.RustNodeIndex(alive)
    kept = idx.load_edges([(s, t, w) for s, tg in graph.items()
                           for t, w in tg.items()])
    assert kept == 4  # dead 端点被预过滤
    got = idx.spreading_channel(list(seeds.items()))

    assert set(ref) == set(got), f"键集不一致: {set(ref) ^ set(got)}"
    for k in ref:
        assert abs(ref[k] - got[k]) < 1e-9, f"{k}: {ref[k]} vs {got[k]}"


def test_rust_spreading_threshold_prunes():
    """act <= threshold 的波前不传播（与 Python 剪枝一致）。"""
    alive = {"s": {"keys": "[]", "text": "", "weight": 1.0},
             "m": {"keys": "[]", "text": "", "weight": 1.0},
             "far": {"keys": "[]", "text": "", "weight": 1.0}}
    # s→m 权重小：hop0 后 m 激活 1.2*0.5*0.08=0.048 <= 阈值 0.05，
    # hop1 时 m 不再外扩 → far 永远拿不到激活
    graph = {"s": {"m": 0.08}, "m": {"far": 1.0}}
    seeds = {"s": 1.2}
    ref = _py_spreading(seeds, alive, graph)
    idx = rust_hybrid.RustNodeIndex(alive)
    idx.load_edges([(s, t, w) for s, tg in graph.items() for t, w in tg.items()])
    got = idx.spreading_channel(list(seeds.items()))
    assert set(ref) == set(got)
    # 剪枝生效：far 未被激活（两侧一致地不含该键）
    assert "far" not in ref and "far" not in got


@pytest.mark.asyncio
async def test_engine_spreading_channel_matches_python(engine, monkeypatch):
    """引擎集成：开关打开时 _spreading_channel 与纯 Python 参考一致。"""
    now = "2026-07-10T12:00:00+08:00"
    for nid, text, keys in [("g1", "节点一", ["甲", "乙"]),
                            ("g2", "节点二", ["丙"]),
                            ("g3", "节点三", ["丁"])]:
        await engine.db.insert_node(
            id=nid, text=text, keys=json.dumps(keys), created=now,
            last_accessed=now, valid_from=now)
    await engine.db.create_edge("g1", "g2", weight=0.7)
    await engine.db.create_edge("g2", "g3", weight=0.5)

    alive = await engine.db.get_alive_nodes()
    graph = {"g1": {"g2": 0.7}, "g2": {"g3": 0.5}}
    seeds = {"g1": 1.0}

    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_ENABLED", True)
    monkeypatch.setattr(rust_hybrid, "RUST_HYBRID_MIN_NODES", 1)
    idx = engine._get_rust_index(alive)
    assert idx is not None
    idx.load_edges([(s, t, w) for s, tg in graph.items() for t, w in tg.items()])
    got = idx.spreading_channel(list(seeds.items()))
    ref = _py_spreading(seeds, alive, graph)
    assert set(ref) == set(got)
    for k in ref:
        assert abs(ref[k] - got[k]) < 1e-9


def test_rust_parser_strict_matches_json_loads():
    """CodeRabbit 研判修复：非法 JSON 形态必须与 json.loads 同拒（尾逗号/裸 token/
    非法转义），合法转义 \b \f 同支持。用 text 不含 key 的探针隔离 keys 解析路径。"""
    def rust_hit(raw, key):
        idx = rust_hybrid.RustNodeIndex(
            {"n0": {"keys": raw, "text": "中性文本", "weight": 1.0},
             "n1": {"keys": '["zzz"]', "text": "t", "weight": 1.0},
             "n2": {"keys": '["zzz"]', "text": "t", "weight": 1.0}})
        return "n0" in idx.direct_channel(key + "查询", [key])

    cases = [
        ('["key",]', 'key', False),        # 尾逗号：json.loads 拒绝
        ('[unquoted]', 'unquoted', False), # 裸 token：拒绝
        ('["key"]', 'key', True),          # 合法：命中
        ('["a\\qb"]', 'aqb', False),     # 非法转义：拒绝
        ('["x\\by\\fz"]', 'x\x08y\x0cz', True),  # \b \f 合法
    ]
    for raw, key, expect in cases:
        assert rust_hit(raw, key) is expect, f"raw={raw!r} 期望 {expect}"


def test_rust_negative_idf_entry_kept():
    """CodeRabbit 研判修复：交集非空但 IDF 为负（单节点 df=n）时，
    Python 保留负值条目，Rust 不得用 >0 过滤丢弃。"""
    idx = rust_hybrid.RustNodeIndex(
        {"n0": {"keys": '["k"]', "text": "k文本", "weight": 1.0}})
    out = idx.direct_channel("k查询", ["k"])
    assert "n0" in out and out["n0"] < 0


def test_rust_zero_activation_seed_entry_kept():
    """CodeRabbit 研判修复：激活值为 0 的种子在 Python defaultdict 中保留
    0.0 条目，Rust 不得用 v!=0 过滤丢弃。"""
    idx = rust_hybrid.RustNodeIndex(
        {"a": {"keys": '["k"]', "text": "a文", "weight": 1.0},
         "b": {"keys": '["z"]', "text": "b文", "weight": 1.0}})
    idx.load_edges([("a", "b", 0.5)])
    out = idx.spreading_channel([("a", 0.0)])
    assert "a" in out and out["a"] == 0.0


def test_parser_strict_matches_json_loads():
    """解析器严格性与 Python json.loads 一致（CodeRabbit 审查项）：
    尾逗号/裸 token/非法转义拒绝；合法转义 \\" \\\\ \\/ \\b \\f 接受。"""
    idx = rust_hybrid.RustNodeIndex({
        "n0": {"keys": '["k"]', "text": "中性", "weight": 1.0},
        "n1": {"keys": '["zzz"]', "text": "t", "weight": 1.0},
        "n2": {"keys": '["zzz"]', "text": "t", "weight": 1.0},
    })
    # 用 n0 的 keys 探针逐个替换，检查命中（text 不含 key，唯一路径是 keys 解析）
    def hit(raw, key):
        idx2 = rust_hybrid.RustNodeIndex({
            "n0": {"keys": raw, "text": "中性文本", "weight": 1.0},
            "n1": {"keys": '["zzz"]', "text": "t", "weight": 1.0},
            "n2": {"keys": '["zzz"]', "text": "t", "weight": 1.0},
        })
        return "n0" in idx2.direct_channel(key + "查询", [key])

    assert hit('["key",]', "key") is False          # 尾逗号拒绝
    assert hit("[unquoted]", "unquoted") is False   # 裸 token 拒绝
    assert hit('["a\\qb"]', "aqb") is False         # 非法转义拒绝
    assert hit('["key"]', "key") is True
    assert hit('["a\\/b"]', "a/b") is True          # \/ 合法
    assert hit('["a\\"b"]', 'a"b') is True          # \" 合法
    assert hit('["\\\\"]', "\\") is True            # \\ 合法


def test_negative_idf_entry_preserved():
    """单节点场景 idf=ln(0.5)<0：Python 保留负值条目，Rust 不得按分数过滤（CodeRabbit 审查项）。"""
    idx = rust_hybrid.RustNodeIndex({
        "n0": {"keys": '["k"]', "text": "k文", "weight": 1.0},
    })
    out = idx.direct_channel("k查询", ["k"])
    assert "n0" in out and out["n0"] < 0


def test_zero_activation_seed_entry_preserved():
    """act=0.0 的种子：Python defaultdict 写入 0 值条目，Rust 不得过滤（CodeRabbit 审查项）。"""
    idx = rust_hybrid.RustNodeIndex({
        "a": {"keys": '["k"]', "text": "a文", "weight": 1.0},
        "b": {"keys": '["z"]', "text": "b文", "weight": 1.0},
    })
    idx.load_edges([("a", "b", 0.5)])
    out = idx.spreading_channel([("a", 0.0)])
    assert "a" in out and out["a"] == 0.0
