"""P0-07: StructuredBlackboard tag/direction 索引过期清理 — 测试"""
from __future__ import annotations

import asyncio

import pytest

from agent_core.structured_blackboard import StructuredBlackboard


@pytest.mark.asyncio
async def test_index_cleanup_on_expiry():
    """After TTL expiry and cleanup, tag/direction indexes should not reference stale keys."""
    bb = StructuredBlackboard(default_ttl=0.1)
    await bb.put_structured("k1", "v1", tags=["t1"], direction="d1", ttl=0.1)
    await bb.put_structured("k2", "v2", tags=["t1"], direction="d2", ttl=60.0)

    assert "k1" in bb._tag_index.get("t1", set())
    assert "k1" in bb._direction_index.get("d1", set())

    await asyncio.sleep(0.15)
    cleaned = await bb.cleanup_expired()

    assert cleaned == 1
    assert "k1" not in bb._tag_index.get("t1", set())
    assert "d1" not in bb._direction_index
    assert "k2" in bb._tag_index.get("t1", set())


@pytest.mark.asyncio
async def test_index_cleanup_empty_tag_removed():
    """When all keys for a tag expire, the tag entry itself should be removed."""
    bb = StructuredBlackboard(default_ttl=0.1)
    await bb.put_structured("k1", "v1", tags=["solo"], ttl=0.1)

    await asyncio.sleep(0.15)
    await bb.cleanup_expired()

    assert "solo" not in bb._tag_index


@pytest.mark.asyncio
async def test_index_cleanup_no_expiry_no_change():
    """When nothing expires, indexes should remain unchanged."""
    bb = StructuredBlackboard(default_ttl=60.0)
    await bb.put_structured("k1", "v1", tags=["t1"], direction="d1", ttl=60.0)

    cleaned = await bb.cleanup_expired()
    assert cleaned == 0
    assert "k1" in bb._tag_index.get("t1", set())
    assert "k1" in bb._direction_index.get("d1", set())
