"""审计 M4（剩余处）：per-provider 并发槽的有界排队 + 排队计入超时预算。"""
from __future__ import annotations

import asyncio

import pytest

from llm_gateway.router_execution import (
    DEFAULT_PROVIDER_QUEUE_TIMEOUT_S,
    _bounded_provider_slot,
    _provider_queue_budget,
)


def test_default_budget(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER_QUEUE_TIMEOUT_S", raising=False)
    assert _provider_queue_budget() == DEFAULT_PROVIDER_QUEUE_TIMEOUT_S == 20.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_QUEUE_TIMEOUT_S", "3.5")
    assert _provider_queue_budget() == 3.5


def test_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_QUEUE_TIMEOUT_S", "abc")
    assert _provider_queue_budget() == DEFAULT_PROVIDER_QUEUE_TIMEOUT_S


@pytest.mark.asyncio
async def test_queue_timeout_raises_and_does_not_leak():
    sem = asyncio.Semaphore(1)
    await sem.acquire()  # 占满
    with pytest.raises(asyncio.TimeoutError):
        async with _bounded_provider_slot(sem, 0.05):
            pass
    assert sem._value == 0  # 没抢到就不该多释放
    sem.release()
    assert sem._value == 1


@pytest.mark.asyncio
async def test_slot_releases_on_body_exception():
    sem = asyncio.Semaphore(1)
    with pytest.raises(ValueError):
        async with _bounded_provider_slot(sem, 1.0):
            raise ValueError("boom")
    assert sem._value == 1


@pytest.mark.asyncio
async def test_waited_time_reported():
    sem = asyncio.Semaphore(1)
    await sem.acquire()

    async def _release_soon():
        await asyncio.sleep(0.05)
        sem.release()

    task = asyncio.create_task(_release_soon())
    async with _bounded_provider_slot(sem, 2.0) as waited:
        assert waited >= 0.04
    await task
    assert sem._value == 1


@pytest.mark.asyncio
async def test_zero_budget_means_unbounded():
    sem = asyncio.Semaphore(1)
    async with _bounded_provider_slot(sem, 0) as waited:
        assert waited >= 0.0
    assert sem._value == 1
