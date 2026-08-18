# tests/workflow_v2/test_migrate.py
from workflow_v2.migrate import migrate_v1
from workflow_v2.models import NodeType


def test_migrate_maps_types_and_chains_edges():
    v1 = {
        "id": "w1", "name": "demo",
        "nodes": [
            {"id": "n1", "type": "tool", "ref": "t.a"},
            {"id": "n2", "type": "skill", "ref": "s.b"},
            {"id": "n3", "type": "model", "ref": "gpt"},
        ],
    }
    rev, report = migrate_v1(v1)
    ids = {n.id: n for n in rev.nodes}
    assert ids["n1"].type == NodeType.TOOL
    # skill -> agent with skill_refs, model -> agent with model_policy
    assert ids["n2"].type == NodeType.AGENT and "s.b" in ids["n2"].config["skill_refs"]
    assert ids["n3"].type == NodeType.AGENT and ids["n3"].config["model_policy"]["ref"] == "gpt"
    # start/end synthesized, linear edges preserved
    assert any(n.type == NodeType.START for n in rev.nodes)
    assert any(n.type == NodeType.END for n in rev.nodes)


def test_migrate_unknown_custom_becomes_legacy_prompt_with_warning():
    v1 = {"id": "w2", "name": "d2", "nodes": [{"id": "c", "type": "custom", "note": "freeform"}]}
    rev, report = migrate_v1(v1)
    node = next(n for n in rev.nodes if n.id == "c")
    assert node.type == NodeType.LEGACY_PROMPT
    assert any(r["node_id"] == "c" and r["warning"] for r in report)
