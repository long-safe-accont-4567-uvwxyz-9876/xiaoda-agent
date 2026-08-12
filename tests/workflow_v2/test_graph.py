import pytest
from workflow_v2.models import NodeSpec, EdgeSpec, NodeType
from workflow_v2.graph import validate_graph, compute_content_hash, GraphError


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
