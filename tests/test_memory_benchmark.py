from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest


def test_ranking_metrics_on_fixed_example() -> None:
    from evaluation.memory_benchmark import mean_reciprocal_rank, ndcg_at_k, recall_at_k

    ranked = ["noise", "relevant_b", "relevant_a"]
    relevant = {"relevant_a", "relevant_b"}

    assert recall_at_k(ranked, relevant, 2) == 0.5
    assert mean_reciprocal_rank(ranked, relevant) == 0.5
    assert ndcg_at_k(ranked, relevant, 3) == pytest.approx(0.6934264)


def test_irrelevant_spread_rate_excludes_seeds() -> None:
    from evaluation.memory_benchmark import irrelevant_spread_rate

    assert irrelevant_spread_rate(
        ranked=["seed", "relevant", "noise_a", "noise_b"],
        relevant={"relevant"},
        seeds={"seed"},
    ) == pytest.approx(2 / 3)


def test_latency_percentiles_use_nearest_rank() -> None:
    from evaluation.memory_benchmark import latency_percentiles

    assert latency_percentiles([1.0, 2.0, 3.0, 4.0, 100.0]) == {
        "p50_ms": 3.0,
        "p95_ms": 100.0,
        "p99_ms": 100.0,
    }


def test_versioned_dataset_has_exactly_120_balanced_cases() -> None:
    from evaluation.memory_benchmark import load_cases

    dataset = load_cases(
        Path(__file__).parents[1] / "evaluation/datasets/memory_v06_cases.json"
    )
    categories = Counter(case["category"] for case in dataset["cases"])

    assert dataset["version"] == "memory-v0.6-phase1.1"
    assert dataset["dataset_kind"] == "synthetic_algorithm_regression"
    assert len(dataset["cases"]) == 120
    assert set(categories) == {
        "direct_fact",
        "one_hop",
        "two_hop",
        "correction",
        "temporal",
        "preference",
        "alias",
        "hub_noise",
        "negative",
        "provenance",
    }
    assert set(categories.values()) == {12}
    assert len({case["id"] for case in dataset["cases"]}) == 120


def test_benchmark_runner_reports_metrics_and_is_deterministic() -> None:
    from evaluation.memory_benchmark import run_benchmark

    cases = [
        {
            "id": "one-hop-01",
            "category": "one_hop",
            "seed_scores": {"seed": 1.0},
            "adjacency": {"seed": {"answer": 1.0, "noise": 0.1}},
            "relevant_ids": ["answer"],
        }
    ]

    first = run_benchmark(cases, k=5, threshold=0.0)
    second = run_benchmark(cases, k=5, threshold=0.0)

    assert first["recall_at_k"] == 1.0
    assert first["mrr"] == 0.5
    assert first["ndcg_at_k"] == pytest.approx(0.6309297535714575)
    assert first["irrelevant_spread_rate"] == 0.5
    assert first["rankings"] == second["rankings"] == [["seed", "answer", "noise"]]
    assert set(first["latency_ms"]) == {"p50_ms", "p95_ms", "p99_ms"}
