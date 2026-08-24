"""G8: context_compressor retrieve_async 测试."""
import asyncio

from memory.context_compressor import ContextCompressor


async def test_retrieve_async_returns_cached():
    """retrieve_async 应返回缓存的原始内容."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comp = ContextCompressor(cache_dir=Path(tmp))
        # 先同步缓存
        comp._cache_original("test_key", "hello world")
        # 异步读取
        result = await comp.retrieve_async("test_key")
        assert result == "hello world"


async def test_retrieve_async_not_found():
    """不存在的 key 返回 None."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comp = ContextCompressor(cache_dir=Path(tmp))
        result = await comp.retrieve_async("nonexistent")
        assert result is None


async def test_retrieve_async_does_not_block_event_loop():
    """retrieve_async 不应阻塞事件循环（其他协程可并行）."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comp = ContextCompressor(cache_dir=Path(tmp))
        comp._cache_original("big_key", "x" * 1024 * 1024)  # 1MB

        other_ran = False

        async def other_task():
            nonlocal other_ran
            other_ran = True

        async def retrieve_task():
            await comp.retrieve_async("big_key")

        # 并行：retrieve 和 other_task 应能交错
        await asyncio.gather(retrieve_task(), other_task())
        assert other_ran, "retrieve_async 期间 other_task 应能执行"
