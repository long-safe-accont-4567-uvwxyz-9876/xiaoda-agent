"""CancelToken 测试 — 超时自动取消 + 主动取消。"""
import asyncio
import pytest
from core.cancel_token import CancelToken, CancellationError


@pytest.mark.asyncio
async def test_cancel_token_not_cancelled_by_default():
    token = CancelToken(timeout=10.0)
    assert not token.is_cancelled
    token.cleanup()


@pytest.mark.asyncio
async def test_cancel_token_explicit_cancel():
    token = CancelToken(timeout=10.0)
    token.cancel("manual")
    assert token.is_cancelled
    assert token.reason == "manual"
    token.cleanup()


@pytest.mark.asyncio
async def test_cancel_token_timeout_auto_cancel():
    token = CancelToken(timeout=0.1)
    await asyncio.sleep(0.2)
    assert token.is_cancelled
    assert "timeout" in token.reason
    token.cleanup()


@pytest.mark.asyncio
async def test_cancel_token_check_raises_when_cancelled():
    token = CancelToken(timeout=10.0)
    token.cancel("manual")
    with pytest.raises(CancellationError):
        token.check()
    token.cleanup()


@pytest.mark.asyncio
async def test_cancel_token_check_passes_when_not_cancelled():
    token = CancelToken(timeout=10.0)
    token.check()
    token.cleanup()


@pytest.mark.asyncio
async def test_cancel_token_with_timeout_none_never_auto_cancel():
    token = CancelToken(timeout=None)
    await asyncio.sleep(0.05)
    assert not token.is_cancelled
    token.cleanup()
