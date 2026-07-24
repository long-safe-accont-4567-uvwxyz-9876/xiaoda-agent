"""SharedBlackboardDB 测试 — SQLite 背板跨进程共享。"""
import asyncio

import pytest

from agent_core.shared_blackboard_db import SharedBlackboardDB


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_blackboard.db")


@pytest.mark.asyncio
async def test_db_blackboard_put_and_get(db_path):
    """写入后能读取。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "value1", agent_name="xiaolang")
    result = await bb.get("key1")
    assert result == "value1"


@pytest.mark.asyncio
async def test_db_blackboard_get_nonexistent(db_path):
    """不存在的 key 返回 None。"""
    bb = SharedBlackboardDB(db_path=db_path)
    result = await bb.get("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_db_blackboard_ttl_expiry(db_path):
    """TTL 过期后返回 None。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "value1", agent_name="xiaolang", ttl=0.1)
    await asyncio.sleep(0.2)
    result = await bb.get("key1")
    assert result is None


@pytest.mark.asyncio
async def test_db_blackboard_cross_process_share(db_path):
    """两个实例共享同一 DB 文件 — 模拟跨进程。"""
    bb1 = SharedBlackboardDB(db_path=db_path)
    bb2 = SharedBlackboardDB(db_path=db_path)
    await bb1.put("shared_key", "shared_value", agent_name="xiaoli")
    result = await bb2.get("shared_key")
    assert result == "shared_value"


@pytest.mark.asyncio
async def test_db_blackboard_get_with_meta(db_path):
    """get_with_meta 返回值和写入者。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "value1", agent_name="xiaoke")
    meta = await bb.get_with_meta("key1")
    assert meta is not None
    assert meta["value"] == "value1"
    assert meta["agent_name"] == "xiaoke"


@pytest.mark.asyncio
async def test_db_blackboard_keys(db_path):
    """keys 返回所有未过期的 key。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "v1", agent_name="a")
    await bb.put("key2", "v2", agent_name="b")
    await bb.put("prefix_key3", "v3", agent_name="c")
    all_keys = await bb.keys()
    assert set(all_keys) >= {"key1", "key2", "prefix_key3"}
    prefix_keys = await bb.keys(prefix="prefix_")
    assert prefix_keys == ["prefix_key3"]


@pytest.mark.asyncio
async def test_db_blackboard_cleanup_expired(db_path):
    """cleanup_expired 清理过期条目。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("expired", "v", agent_name="a", ttl=0.1)
    await bb.put("permanent", "v", agent_name="b", ttl=3600)
    await asyncio.sleep(0.2)
    cleaned = await bb.cleanup_expired()
    assert cleaned == 1
    assert await bb.get("expired") is None
    assert await bb.get("permanent") == "v"
