# workflow_v2/migrate.py
from __future__ import annotations
import time
import uuid
from workflow_v2.models import NodeSpec, EdgeSpec, NodeType, WorkflowRevision
from workflow_v2.graph import compute_content_hash


def _convert(node: dict) -> tuple[NodeSpec, dict | None]:
    nid = node["id"]
    t = node.get("type", "step")
    ref = node.get("ref", "")
    if t == "tool":
        return NodeSpec(id=nid, type=NodeType.TOOL, name=node.get("label", nid),
                        config={"tool_ref": ref, "arguments": node.get("params", {})}), None
    if t == "mcp":
        return NodeSpec(id=nid, type=NodeType.MCP, name=node.get("label", nid),
                        config={"tool_ref": ref, "arguments": node.get("params", {})}), None
    if t == "agent":
        return NodeSpec(id=nid, type=NodeType.AGENT, name=node.get("label", nid),
                        config={"agent_ref": ref}), None
    if t == "skill":
        return NodeSpec(id=nid, type=NodeType.AGENT, name=node.get("label", nid),
                        config={"skill_refs": [ref]}), None
    if t == "model":
        return NodeSpec(id=nid, type=NodeType.AGENT, name=node.get("label", nid),
                        config={"model_policy": {"ref": ref}}), None
    if t == "step":
        return NodeSpec(id=nid, type=NodeType.TRANSFORM, name=node.get("label", nid),
                        config={"note": node.get("note", "")}), None
    # unknown/custom -> legacy_prompt with warning
    warn = {"node_id": nid, "action": "legacy_prompt", "warning": f"unmapped v1 type '{t}', needs manual review"}
    return NodeSpec(id=nid, type=NodeType.LEGACY_PROMPT, name=node.get("label", nid),
                    config={"raw": node}), warn


def migrate_v1(v1: dict) -> tuple[WorkflowRevision, list[dict]]:
    report: list[dict] = []
    inner: list[NodeSpec] = []
    for n in v1.get("nodes", []) or []:
        spec, warn = _convert(n)
        inner.append(spec)
        if warn:
            report.append(warn)

    start = NodeSpec(id="__start__", type=NodeType.START, name="start")
    end = NodeSpec(id="__end__", type=NodeType.END, name="end")
    nodes = [start, *inner, end]

    edges: list[EdgeSpec] = []
    chain = [start.id, *[s.id for s in inner], end.id]
    for a, b in zip(chain, chain[1:]):
        edges.append(EdgeSpec(source=a, target=b))

    ch = compute_content_hash(nodes, edges, {})
    rev = WorkflowRevision(
        revision_id=f"wfr_{uuid.uuid4().hex[:12]}",
        workflow_id=v1.get("id", ""), nodes=nodes, edges=edges,
        content_hash=ch, created_at=time.time(),
    )
    return rev, report
