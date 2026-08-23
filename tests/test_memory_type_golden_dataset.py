from __future__ import annotations

import inspect
from collections import Counter

from evaluation.eval_memory_type_classification import (
    DEFAULT_DATASET,
    TARGET_ACCURACY,
    load_dataset,
)
from memory import enrichment
from memory._memory_encoder import MemoryEncoder


def test_golden_dataset_is_versioned_balanced_and_well_formed():
    items = load_dataset(DEFAULT_DATASET)
    counts = Counter(item["expected"] for item in items)

    assert len(items) >= 45
    assert len({item["id"] for item in items}) == len(items)
    assert set(counts) == enrichment.MEMORY_TYPES
    assert min(counts.values()) >= 8
    for item in items:
        assert len(item["exchanges"]) >= 2
        assert {message["role"] for message in item["exchanges"]} <= {
            "user", "assistant"
        }
        assert any(message["role"] == "user" for message in item["exchanges"])


def test_production_and_evaluator_share_the_same_prompt_builder():
    production = inspect.getsource(MemoryEncoder._enrich_memory_async)
    evaluator = inspect.getsource(
        __import__("evaluation.eval_memory_type_classification", fromlist=["evaluate"]).evaluate
    )

    assert "build_classification_prompt(text)" in production
    assert "build_classification_prompt(text)" in evaluator
    assert TARGET_ACCURACY == 0.90


def test_prompt_has_strict_five_type_contract():
    prompt = enrichment.build_classification_prompt("用户: 示例")
    assert "fact/event/affect/relation/instruction 五选一" in prompt
    assert '"importance": 0.0' in prompt
