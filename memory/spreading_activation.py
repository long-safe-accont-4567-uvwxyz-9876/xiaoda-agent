"""Deterministic bounded spreading activation for memory candidates."""

from __future__ import annotations

from collections.abc import Mapping
from math import sqrt


def spread_activation(
    seed_scores: Mapping[str, float],
    adjacency: Mapping[str, Mapping[str, float]],
    max_hops: int = 1,
    decay: float = 0.5,
    threshold: float = 0.05,
    candidate_budget: int = 50,
) -> dict[str, float]:
    """Return seed scores plus activation propagated over weighted edges."""
    scores = {node: float(score) for node, score in seed_scores.items()}
    frontier = dict(scores)

    for hop in range(1, max(0, max_hops) + 1):
        next_frontier: dict[str, float] = {}
        for source, activation in sorted(frontier.items()):
            neighbors = adjacency.get(source, {})
            degree_penalty = 1.0 / sqrt(max(1, len(neighbors)))
            for target, weight in sorted(neighbors.items()):
                propagated = (
                    float(activation)
                    * float(weight)
                    * decay
                    * degree_penalty
                    / hop
                )
                if propagated >= threshold:
                    next_frontier[target] = next_frontier.get(target, 0.0) + propagated
        for target, activation in next_frontier.items():
            scores[target] = scores.get(target, 0.0) + activation
        frontier = next_frontier
        if not frontier:
            break

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[: max(0, candidate_budget)])
