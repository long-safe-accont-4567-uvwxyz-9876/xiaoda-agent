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


async def test_single_flight_returns_none_on_exception(tmp_path, patched_xiaoda_voice):
    """leader 抛异常时，所有调用者返回 None（Path | None 契约），且 _inflight 已清空、上游只打一次。"""
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
        # CodeRabbit: synthesize 返回类型是 Path | None，异常时返回 None 而非抛异常
        assert r is None, f"异常时应返回 None，实际 {type(r).__name__}: {r}"
    assert engine._inflight == {}, "异常路径下 in-flight 表也应清空"


async def test_single_flight_cancelled_error_cleans_up_inflight(tmp_path, patched_xiaoda_voice):
    """CancelledError 时 _inflight 必须清理，否则后续相同文本的请求永久挂起。

    根因：原代码 finally 块中 fut.exception() 对未 done 的 future 抛 InvalidStateError，
    导致 _inflight.pop 不执行，残留的 orphaned future 使后续请求 await shield 永久阻塞。
    修复：CancelledError 时先 cancel future，finally 中安全处理未 done 的 future。
    """
    engine = _make_engine(tmp_path)

    async def fake_upstream(voice, voice_data_url, messages):
        # 模拟长时间上游调用，让外部有足够时间取消
        await asyncio.sleep(5)
        return _make_completion()

    async def fake_encode(path):
        return "data:audio/wav;base64,AAAA"

    with patch.object(engine, "_call_tts_with_retry", AsyncMock(side_effect=fake_upstream)), \
         patch.object(tts_engine, "_encode_voice_file", AsyncMock(side_effect=fake_encode)):
        # 启动合成并在 0.1s 后取消
        task = asyncio.create_task(engine.synthesize_xiaoda("取消测试文本"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # 关键断言：_inflight 必须为空，否则后续请求会永久挂起
    assert engine._inflight == {}, f"CancelledError 后 _inflight 应清空，实际残留 {engine._inflight}"

    # 验证后续请求不会挂起：新请求应正常执行
    async def fake_upstream_2(voice, voice_data_url, messages):
        return _make_completion()

    with patch.object(engine, "_call_tts_with_retry", AsyncMock(side_effect=fake_upstream_2)), \
         patch.object(tts_engine, "_encode_voice_file", AsyncMock(side_effect=fake_encode)):
        result = await asyncio.wait_for(
            engine.synthesize_xiaoda("取消测试文本"),
            timeout=2.0,
        )
    assert isinstance(result, Path), f"取消后重试应成功，实际 {type(result)}"
