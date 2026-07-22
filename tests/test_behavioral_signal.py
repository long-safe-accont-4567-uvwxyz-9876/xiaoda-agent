# tests/test_behavioral_signal.py

import pytest

from core.behavioral_signal import BehavioralSignalStream


@pytest.mark.asyncio
async def test_emit_and_get_history():
    stream = BehavioralSignalStream(max_history=100)
    await stream.emit("confidence", 0.8, "test_source")
    history = stream.get_history("confidence", last_n=10)
    assert len(history) == 1
    assert history[0].signal_type == "confidence"
    assert history[0].value == 0.8
    assert history[0].source == "test_source"


@pytest.mark.asyncio
async def test_emit_with_meta():
    stream = BehavioralSignalStream()
    await stream.emit("tool_usage", 0.5, "tool_executor", agent="xiaolang", tool="read")
    history = stream.get_history("tool_usage")
    assert history[0].meta["agent"] == "xiaolang"
    assert history[0].meta["tool"] == "read"


@pytest.mark.asyncio
async def test_subscribe_notification():
    stream = BehavioralSignalStream()
    ev = await stream.subscribe("confidence")
    assert not ev.is_set()
    await stream.emit("confidence", 0.9, "test")
    assert ev.is_set()


@pytest.mark.asyncio
async def test_aggregate_mean_of_means():
    stream = BehavioralSignalStream()
    for v in [0.2, 0.4, 0.6, 0.8]:
        await stream.emit("confidence", v, "test")
    result = stream.aggregate("confidence", "mean_of_means")
    assert abs(result - 0.5) < 0.001


@pytest.mark.asyncio
async def test_aggregate_max_of_means():
    stream = BehavioralSignalStream()
    for v in [0.2, 0.4, 0.6, 0.8]:
        await stream.emit("confidence", v, "test")
    result = stream.aggregate("confidence", "max_of_means")
    assert abs(result - 0.8) < 0.001


@pytest.mark.asyncio
async def test_aggregate_max_absolute():
    stream = BehavioralSignalStream()
    for v in [-0.3, 0.5, -0.8, 0.2]:
        await stream.emit("sentiment", v, "test")
    result = stream.aggregate("sentiment", "max_absolute")
    assert abs(result - 0.8) < 0.001


@pytest.mark.asyncio
async def test_aggregate_empty_returns_zero():
    stream = BehavioralSignalStream()
    result = stream.aggregate("nonexistent", "mean_of_means")
    assert result == 0.0


@pytest.mark.asyncio
async def test_max_history_deque():
    stream = BehavioralSignalStream(max_history=3)
    for i in range(5):
        await stream.emit("confidence", float(i), "test")
    history = stream.get_history("confidence")
    assert len(history) == 3
    assert history[0].value == 2.0
    assert history[-1].value == 4.0


@pytest.mark.asyncio
async def test_get_history_all_types():
    stream = BehavioralSignalStream()
    await stream.emit("confidence", 0.5, "test")
    await stream.emit("sentiment", 0.3, "test")
    history = stream.get_history(last_n=10)
    assert len(history) == 2
