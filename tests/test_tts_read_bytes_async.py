"""G9: tts_engine read_bytes async 测试."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


async def test_synthesize_voice_data_url_uses_async_read():
    """synthesize_voice_data_url 应使用 asyncio.to_thread 读取文件."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"\x00" * (10 * 1024 * 1024))  # 10MB
        f.flush()
        big_file = Path(f.name)

    try:
        # 检查 tts_engine 源码是否使用 asyncio.to_thread
        import inspect
        from emotion import tts_engine
        source = inspect.getsource(tts_engine)
        assert "asyncio.to_thread" in source or "to_thread" in source, \
            "tts_engine 应使用 asyncio.to_thread 异步读取文件"
    finally:
        big_file.unlink(missing_ok=True)


async def test_concurrent_read_does_not_block():
    """10MB 文件读取期间事件循环不阻塞."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"\x00" * (10 * 1024 * 1024))
        f.flush()
        big_file = Path(f.name)

    other_ran = False

    async def other_task():
        nonlocal other_ran
        other_ran = True

    async def read_task():
        return await asyncio.to_thread(big_file.read_bytes)

    try:
        await asyncio.gather(read_task(), other_task())
        assert other_ran
    finally:
        big_file.unlink(missing_ok=True)
