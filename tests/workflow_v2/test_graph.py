import pytest

from workflow_v2.graph import GraphError, compute_content_hash, validate_graph
from workflow_v2.models import EdgeSpec, NodeSpec, NodeType


def _n(nid, ntype=NodeType.TOOL):
    return NodeSpec(id=nid, type=ntype, name=nid, config={"tool_ref": "t"})


def test_valid_linear_graph_passes():
    nodes = [_n("start", NodeType.START), _n("a"), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    validate_graph(nodes, edges)  # no raise


def test_cycle_rejected():
    nodes = [_n("start", NodeType.START), _n("a"), _n("b"), _n("end", NodeType.END)]
    edges = [
        EdgeSpec(source="start", target="a"),
        EdgeSpec(source="a", target="b"),
        EdgeSpec(source="b", target="a"),
    ]
    with pytest.raises(GraphError) as exc:
        validate_graph(nodes, edges)
    assert exc.value.code == "WORKFLOW_REVISION_INVALID"
    assert "cycle" in exc.value.details


def test_unreachable_node_rejected():
    nodes = [_n("start", NodeType.START), _n("a"), _n("orphan"), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    with pytest.raises(GraphError) as exc:
        validate_graph(nodes, edges)
    assert "orphan" in exc.value.details.get("unreachable", [])


def test_dangling_edge_rejected():
    nodes = [_n("start", NodeType.START), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="ghost")]
    with pytest.raises(GraphError):
        validate_graph(nodes, edges)


def test_content_hash_stable_and_order_independent():
    nodes = [_n("start", NodeType.START), _n("a"), _n("end", NodeType.END)]
    edges = [EdgeSpec(source="start", target="a"), EdgeSpec(source="a", target="end")]
    h1 = compute_content_hash(nodes, edges, {})
    h2 = compute_content_hash(list(reversed(nodes)), list(reversed(edges)), {})
    assert h1 == h2 and len(h1) == 64


def test_content_hash_mixed_conditional_edges():
    # condition-node branches mix unconditional (condition=None) and
    # conditional (condition="<expr>") edges to the same target (default
    # branch + guarded branch); the hash must handle both.
    nodes = [_n("start", NodeType.START), _n("a"), _n("b"), _n("end", NodeType.END)]
    edges = [
        EdgeSpec(source="start", target="a"),
        EdgeSpec(source="a", target="b", condition="c > 1"),
        EdgeSpec(source="a", target="b"),
        EdgeSpec(source="b", target="end"),
    ]
    validate_graph(nodes, edges)  # must be a valid revision
    h1 = compute_content_hash(nodes, edges, {})
    h2 = compute_content_hash(list(reversed(nodes)), list(reversed(edges)), {})
    assert len(h1) == 64
    assert h1 == h2


def test_validate_graph_rejects_unimplemented_node_types():
    """六种未实现节点类型必须在编译期拒绝（此前运行时才报 UNSUPPORTED_NODE）。"""
    nodes = [
        _n("start", NodeType.START),
        NodeSpec(id="wait", type=NodeType.DELAY, name="w"),
        _n("end", NodeType.END),
    ]
    edges = [EdgeSpec(source="start", target="wait"),
             EdgeSpec(source="wait", target="end")]
    with pytest.raises(GraphError) as ei:
        validate_graph(nodes, edges)
    assert "unsupported node type" in str(ei.value)
    assert "delay" in ei.value.details["unsupported_types"]
    assert ei.value.details["nodes"] == ["wait"]


def test_validate_graph_accepts_all_supported_types():
    from workflow_v2.graph import SUPPORTED_NODE_TYPES
    # 未实现集合与审计清单一致，防止有人悄悄扩 executor 却漏更新语义
    implemented = {t.value for t in NodeType} - {
        "condition", "parallel", "join", "workflow", "input", "delay"}
    assert {t.value for t in SUPPORTED_NODE_TYPES} == implemented
