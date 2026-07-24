"""CancelToken 测试 — 超时自动取消 + 主动取消。"""
import asyncio

import pytest

from core.cancel_token import CancellationError, CancelToken


@pytest.fixture
def cancel_token():
    """Provide a CancelToken that auto-cleanup on test end."""
    tokens = []

    class _Factory:
        def create(self, **kwargs):
            t = CancelToken(**kwargs)
            tokens.append(t)
            return t

    yield _Factory()
    for t in tokens:
        try:
            t.cleanup()
        except RuntimeError:
            pass  # event loop already closed


@pytest.mark.asyncio
async def test_cancel_token_not_cancelled_by_default(cancel_token):
    token = cancel_token.create(timeout=10.0)
    assert not token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_token_explicit_cancel(cancel_token):
    token = cancel_token.create(timeout=10.0)
    token.cancel("manual")
    assert token.is_cancelled
    assert token.reason == "manual"


@pytest.mark.asyncio
async def test_cancel_token_timeout_auto_cancel(cancel_token):
    token = cancel_token.create(timeout=0.1)
    await asyncio.sleep(0.2)
    assert token.is_cancelled
    assert "timeout" in token.reason


@pytest.mark.asyncio
async def test_cancel_token_check_raises_when_cancelled(cancel_token):
    token = cancel_token.create(timeout=10.0)
    token.cancel("manual")
    with pytest.raises(CancellationError):
        token.check()


@pytest.mark.asyncio
async def test_cancel_token_check_passes_when_not_cancelled(cancel_token):
    token = cancel_token.create(timeout=10.0)
    token.check()


@pytest.mark.asyncio
async def test_cancel_token_with_timeout_none_never_auto_cancel(cancel_token):
    token = cancel_token.create(timeout=None)
    await asyncio.sleep(0.05)
    assert not token.is_cancelled
