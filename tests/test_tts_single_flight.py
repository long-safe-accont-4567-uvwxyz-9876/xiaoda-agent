"""TTS single-flight + 上游并发上界测试。

验证缓存未命中时，相同 cache_key 的并发请求只触发一次上游调用（缓存击穿防护），
所有等待者共享同一结果或同一异常，且 _inflight 在结束后被清理。
"""
import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from emotion import tts_engine


def _make_completion() -> MagicMock:
    """构造一个 audio.data 合法（解码后 >= 1024 字节）的 completion mock。"""
    audio = MagicMock()
    audio.data = base64.b64encode(b"\x00" * 2048).decode("ascii")
    message = MagicMock()
    message.audio = audio
    choice = MagicMock()
    choice.message = message
    completion = MagicMock()
    completion.choices = [choice]
    return completion


def _make_engine(tmp_path: Path) -> tts_engine.TTSEngine:
    """构造一个绕过 init/健康检查的可用引擎实例。"""
    engine = tts_engine.TTSEngine()
    # available 属性要求 _available 且 _client 非 None
    engine._available = True
    engine._client = MagicMock()
    engine._output_dir = tmp_path
    engine._cache_index_path = None  # 跳过 _save_cache_index 的磁盘写入
    return engine


@pytest.fixture
def patched_xiaoda_voice(tmp_path):
    """让 VOICE_REFERENCES['xiaoda'] 指向真实存在的临时文件，绕过 voice_path.exists() 检查。"""
    wav = tmp_path / "xiaoda.wav"
    wav.write_bytes(b"\x00" * 16)
    with patch.dict(tts_engine.VOICE_REFERENCES, {"xiaoda": wav}, clear=False):
        yield


async def test_single_flight_coalesces_20_identical_requests(tmp_path, patched_xiaoda_voice):
    """20 个相同请求只打一次上游，且返回同一个 Path，_inflight 清空。"""
    engine = _make_engine(tmp_path)

    call_count = 0

    async def fake_upstream(voice, voice_data_url, messages):
        nonlocal call_count
        call_count += 1
        # 让出事件循环，使其余等待者在 leader 仍在途时注册到同一 future
        await asyncio.sleep(0.05)
        return _make_completion()

    async def fake_encode(path):
        return "data:audio/wav;base64,AAAA"

    with patch.object(engine, "_call_tts_with_retry", AsyncMock(side_effect=fake_upstream)), \
         patch.object(tts_engine, "_encode_voice_file", AsyncMock(side_effect=fake_encode)):
        results = await asyncio.gather(
            *[engine.synthesize_xiaoda("同一句话") for _ in range(20)]
        )

    assert call_count == 1, f"上游应只调用一次（缓存击穿防护），实际 {call_count}"
    assert len(results) == 20
    assert all(r == results[0] for r in results), "20 个等待者应拿到同一返回值"
    assert isinstance(results[0], Path)
    assert results[0].exists(), "音频文件应已写入磁盘"
    assert engine._inflight == {}, "in-flight 表结束后应清空"


async def test_single_flight_propagates_exception_to_all_waiters(tmp_path, patched_xiaoda_voice):
    """leader 抛异常时，20 个等待者都拿到同一异常，且 _inflight 已清空、上游只打一次。"""
    engine = _make_engine(tmp_path)

    call_count = 0

    async def fake_upstream(voice, voice_data_url, messages):
        nonlocal call_count
        call_count += 1
        # 先让出事件循环让等待者注册到 future，再抛异常
        await asyncio.sleep(0.05)
        raise RuntimeError("upstream boom")

    async def fake_encode(path):
        return "data:audio/wav;base64,AAAA"

    with patch.object(engine, "_call_tts_with_retry", AsyncMock(side_effect=fake_upstream)), \
         patch.object(tts_engine, "_encode_voice_file", AsyncMock(side_effect=fake_encode)):
        gathered = await asyncio.gather(
            *[engine.synthesize_xiaoda("同一句话") for _ in range(20)],
            return_exceptions=True,
        )

    assert call_count == 1, f"上游应只调用一次，实际 {call_count}"
    assert len(gathered) == 20
    for r in gathered:
        assert isinstance(r, RuntimeError), f"等待者应拿到 RuntimeError，实际 {type(r).__name__}: {r}"
        assert "upstream boom" in str(r)
    assert engine._inflight == {}, "异常路径下 in-flight 表也应清空"
