"""Deterministic metrics and runner for the memory v0.6 benchmark."""

from __future__ import annotations

import json
from collections.abc import Sequence
from math import ceil, log2
from pathlib import Path
from time import perf_counter
from typing import Any

from memory.spreading_activation import spread_activation


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def mean_reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for rank, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / log2(rank + 1) for rank, item in enumerate(ranked[:k], 1) if item in relevant)
    ideal_count = min(len(relevant), k)
    if ideal_count == 0:
        return 1.0
    ideal = sum(1.0 / log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / ideal


def irrelevant_spread_rate(
    ranked: Sequence[str], relevant: set[str], seeds: set[str]
) -> float:
    expanded = [item for item in ranked if item not in seeds]
    if not expanded:
        return 0.0
    return sum(item not in relevant for item in expanded) / len(expanded)


def latency_percentiles(latencies_ms: Sequence[float]) -> dict[str, float]:
    if not latencies_ms:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}
    values = sorted(float(value) for value in latencies_ms)

    def nearest_rank(percentile: float) -> float:
        index = max(0, ceil(percentile * len(values)) - 1)
        return values[index]

    return {
        "p50_ms": nearest_rank(0.50),
        "p95_ms": nearest_rank(0.95),
        "p99_ms": nearest_rank(0.99),
    }


def load_cases(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        dataset = json.load(handle)
    if not isinstance(dataset, dict):
        raise ValueError("benchmark dataset must be an object")
    if "cases" not in dataset:
        generator = dataset.get("generator", {})
        categories = generator.get("categories", [])
        count = int(generator.get("count_per_category", 0))
        dataset["cases"] = [
            {
                "id": f"{category}-{index:02d}",
                "category": category,
                "query": f"memory v0.6 {category} query {index:02d}",
                "seed_scores": {f"{category}-seed-{index:02d}": 1.0},
                "adjacency": {
                    f"{category}-seed-{index:02d}": {
                        f"{category}-answer-{index:02d}": 1.0,
                        f"noise-hub-{index % 3}": 0.1,
                    }
                },
                "relevant_ids": [f"{category}-answer-{index:02d}"],
                "provenance_ids": [f"source-{category}-{index:02d}"],
            }
            for category in categories
            for index in range(1, count + 1)
        ]
    if not isinstance(dataset.get("cases"), list):
        raise ValueError("benchmark dataset must contain a cases list")
    return dataset


def run_benchmark(
    cases: Sequence[dict[str, Any]],
    *,
    k: int = 5,
    max_hops: int = 1,
    decay: float = 0.5,
    threshold: float = 0.05,
    candidate_budget: int = 50,
) -> dict[str, Any]:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    irrelevant_rates: list[float] = []
    latencies: list[float] = []
    rankings: list[list[str]] = []

    for case in cases:
        started = perf_counter()
        scores = spread_activation(
            case["seed_scores"],
            case["adjacency"],
            max_hops=max_hops,
            decay=decay,
            threshold=threshold,
            candidate_budget=candidate_budget,
        )
        latencies.append((perf_counter() - started) * 1000.0)
        ranked = list(scores)
        rankings.append(ranked)
        relevant = set(case.get("relevant_ids", []))
        seeds = set(case["seed_scores"])
        recalls.append(recall_at_k(ranked, relevant, k))
        reciprocal_ranks.append(mean_reciprocal_rank(ranked, relevant))
        ndcgs.append(ndcg_at_k(ranked, relevant, k))
        irrelevant_rates.append(irrelevant_spread_rate(ranked, relevant, seeds))

    count = len(cases)
    def _mean(values): return sum(values) / count if count else 0.0
    return {
        "case_count": count,
        "recall_at_k": _mean(recalls),
        "mrr": _mean(reciprocal_ranks),
        "ndcg_at_k": _mean(ndcgs),
        "irrelevant_spread_rate": _mean(irrelevant_rates),
        "latency_ms": latency_percentiles(latencies),
        "rankings": rankings,
    }
