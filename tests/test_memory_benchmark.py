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


def test_ndcg_empty_relevant_set_scores_zero_not_vacuous_full():
    from evaluation.memory_benchmark import ndcg_at_k

    assert ndcg_at_k(["a", "b", "c"], set(), 3) == 0.0


def test_ndcg_perfect_topk_scores_full_even_when_relevant_exceeds_k():
    """|relevant|>k 时 ideal 按 min(len(relevant), k) 截断（对齐 web._ndcg）。

    守卫 4a7721d4 曾顺带引入的截断语义回退：不截断会导致 top-k 全命中
    也拿不到 1.0（被 k 截不到的 relevant 项拖累分母）。
    """
    import math

    from evaluation.memory_benchmark import ndcg_at_k

    # top-k 全为相关且 |relevant|>k：截断后满分（回归守卫核心，
    # 未截断的旧公式此处只得 ~0.879）
    assert ndcg_at_k(["a", "b"], {"a", "b", "c"}, 2) == 1.0
    assert ndcg_at_k(["b", "c", "a", "x"], {"a", "b", "c", "d"}, 3) == 1.0
    # 部分命中：分母同样只按前 k 个理想位累计
    expected = (1.0 / math.log2(3)) / (1.0 + 1.0 / math.log2(3))
    assert ndcg_at_k(["x", "a"], {"a", "b"}, 2) == pytest.approx(expected)
    # k 容得下全部 relevant 但漏检一项：标准语义下受罚
    assert ndcg_at_k(["a"], {"a", "b"}, 5) == pytest.approx(
        1.0 / (1.0 + 1.0 / math.log2(3)))
