"""审计 M4（部分）：per-provider LLM 并发上限从写死 3 改为可配。

背景（2026-11-08 只读实测）：model_router.py 的 MAX_PROVIDER_CONCURRENCY=3
被所有 provider 共用，而各家配额不同（兼容网关 1、本地 vLLM 可到 8）。
本测试固定两件事：默认仍是 3（现网行为不变），以及环境变量能按 provider 覆盖。
"""
from __future__ import annotations

import model_router as mr


def test_default_limit_is_three(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER_CONCURRENCY_AGNES", raising=False)
    assert mr.MAX_PROVIDER_CONCURRENCY == 3
    assert mr.provider_concurrency_limit("agnes") == 3


def test_env_override_per_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CONCURRENCY_LOCAL_VLLM", "8")
    assert mr.provider_concurrency_limit("local-vllm") == 8
    # 其他 provider 不受影响
    assert mr.provider_concurrency_limit("agnes") == 3


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CONCURRENCY_AGNES", "abc")
    assert mr.provider_concurrency_limit("agnes") == 3
    monkeypatch.setenv("LLM_PROVIDER_CONCURRENCY_AGNES", "0")
    assert mr.provider_concurrency_limit("agnes") == 1  # 至少 1


def test_semaphore_created_with_provider_limit(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CONCURRENCY_AGNES", "5")
    router = mr.ModelRouter.__new__(mr.ModelRouter)  # 懒创建的信号量表
    assert router._get_provider_call_semaphore("agnes")._value == 5
    # 同一 provider 复用同一个信号量
    assert router._get_provider_call_semaphore("agnes")._value == 5
