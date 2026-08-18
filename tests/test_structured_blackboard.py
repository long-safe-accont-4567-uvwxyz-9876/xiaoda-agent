# tests/test_structured_blackboard.py
import pytest
import asyncio
import time
from agent_core.structured_blackboard import StructuredEntry, StructuredBlackboard


@pytest.mark.asyncio
async def test_put_structured_basic():
    bb = StructuredBlackboard()
    await bb.put_structured("key1", "value1", agent_name="xiaoda")
    val = await bb.get("key1")
    assert val == "value1"


@pytest.mark.asyncio
async def test_put_structured_with_tags():
    bb = StructuredBlackboard()
    await bb.put_structured("key1", "value1", agent_name="xiaoda", tags=["memory", "fact"])
    await bb.put_structured("key2", "value2", agent_name="xiaoda", tags=["memory"])
    results = await bb.query_by_tag("memory")
    assert len(results) == 2
    results_fact = await bb.query_by_tag("fact")
    assert len(results_fact) == 1


@pytest.mark.asyncio
async def test_put_structured_with_direction():
    bb = StructuredBlackboard()
    await bb.put_structured("key1", "value1", agent_name="xiaoda", direction="calm")
    results = await bb.query_by_direction("calm")
    assert len(results) == 1
    assert results[0]["key"] == "key1"


@pytest.mark.asyncio
async def test_query_by_tag_empty():
    bb = StructuredBlackboard()
    results = await bb.query_by_tag("nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_query_by_direction_empty():
    bb = StructuredBlackboard()
    results = await bb.query_by_direction("nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_merge_from():
    bb1 = StructuredBlackboard()
    bb2 = StructuredBlackboard()
    await bb2.put("key1", "value1")
    await bb2.put("key2", "value2")
    merged = await bb1.merge_from(bb2)
    assert merged == 2
    assert await bb1.get("key1") == "value1"
    assert await bb1.get("key2") == "value2"


@pytest.mark.asyncio
async def test_merge_from_skip_existing():
    bb1 = StructuredBlackboard()
    bb2 = StructuredBlackboard()
    await bb1.put("key1", "original")
    await bb2.put("key1", "should_not_overwrite")
    await bb2.put("key2", "new_value")
    merged = await bb1.merge_from(bb2)
    assert merged == 1
    assert await bb1.get("key1") == "original"
    assert await bb1.get("key2") == "new_value"
