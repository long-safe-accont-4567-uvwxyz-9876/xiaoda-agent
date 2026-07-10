from __future__ import annotations


def test_one_hop_propagates_weighted_activation() -> None:
    from memory.spreading_activation import spread_activation

    scores = spread_activation(
        {"seed": 1.0},
        {"seed": {"related": 0.8}},
        decay=0.5,
        threshold=0.0,
    )

    assert scores == {"seed": 1.0, "related": 0.4}


def test_candidate_budget_keeps_highest_scores_with_stable_ties() -> None:
    from memory.spreading_activation import spread_activation

    scores = spread_activation(
        {"seed": 1.0},
        {"seed": {"z": 0.8, "b": 0.9, "a": 0.9}},
        threshold=0.0,
        candidate_budget=3,
    )

    assert list(scores) == ["seed", "a", "b"]


def test_high_degree_sources_receive_a_degree_penalty() -> None:
    from memory.spreading_activation import spread_activation

    scores = spread_activation(
        {"hub": 1.0, "focused": 1.0},
        {
            "hub": {"hub_target": 1.0, "noise_1": 0.1, "noise_2": 0.1, "noise_3": 0.1},
            "focused": {"focused_target": 1.0},
        },
        threshold=0.0,
    )

    assert scores["focused_target"] == 0.5
    assert scores["hub_target"] == 0.25


def test_max_hops_bounds_propagation_and_uses_hop_decay() -> None:
    from memory.spreading_activation import spread_activation

    adjacency = {"a": {"b": 1.0}, "b": {"c": 1.0}}

    one_hop = spread_activation({"a": 1.0}, adjacency, threshold=0.0)
    two_hops = spread_activation({"a": 1.0}, adjacency, max_hops=2, threshold=0.0)

    assert "c" not in one_hop
    assert two_hops["c"] == 0.125
