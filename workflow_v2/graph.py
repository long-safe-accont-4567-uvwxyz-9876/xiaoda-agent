from __future__ import annotations
import hashlib
import json
from workflow_v2.models import NodeSpec, EdgeSpec, NodeType


class GraphError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = "WORKFLOW_REVISION_INVALID"
        self.details = details or {}


def validate_graph(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> None:
    ids = [n.id for n in nodes]
    id_set = set(ids)
    if len(ids) != len(id_set):
        raise GraphError("duplicate node id", {"duplicate": True})

    starts = [n.id for n in nodes if n.type == NodeType.START]
    ends = [n.id for n in nodes if n.type == NodeType.END]
    if len(starts) != 1:
        raise GraphError("exactly one start required", {"starts": starts})
    if not ends:
        raise GraphError("at least one end required", {"ends": ends})

    adj: dict[str, list[str]] = {i: [] for i in id_set}
    for e in edges:
        if e.source not in id_set or e.target not in id_set:
            raise GraphError("dangling edge", {"edge": [e.source, e.target]})
        adj[e.source].append(e.target)

    # cycle detection (DFS colors)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in id_set}

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in adj[u]:
            if color[v] == GRAY:
                raise GraphError("cycle detected", {"cycle": [u, v]})
            if color[v] == WHITE:
                dfs(v)
        color[u] = BLACK

    for nid in id_set:
        if color[nid] == WHITE:
            dfs(nid)

    # reachability from start
    seen: set[str] = set()
    stack = [starts[0]]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj[cur])
    unreachable = sorted(id_set - seen)
    if unreachable:
        raise GraphError("unreachable nodes", {"unreachable": unreachable})


def compute_content_hash(nodes: list[NodeSpec], edges: list[EdgeSpec], input_schema: dict) -> str:
    # Normalize condition None -> "" once, then sort and serialize the same
    # tuples so the hash is deterministic and order-independent even when a
    # revision mixes unconditional (condition=None) and conditional edges.
    normalized_edges = [
        (e.source, e.target, "" if e.condition is None else e.condition) for e in edges
    ]
    payload = {
        "nodes": sorted((n.model_dump(mode="json") for n in nodes), key=lambda x: x["id"]),
        "edges": sorted(normalized_edges),
        "input_schema": input_schema,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
