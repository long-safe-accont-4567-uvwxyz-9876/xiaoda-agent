"""B2 重构契约测试：_test_* 函数族模板化。

原 10 个 _test_* 函数结构几乎一致（httpx client → request → check status →
return (bool, str)），重构为 _run_api_test 通用模板 + 各 provider 声明。
契约：10 个 provider 的测试函数返回值与重构前完全一致。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.mark.asyncio
async def test_test_mimo_success():
    """MiMo 200 + choices → (True, 成功消息)。"""
    from web.routers import setup as mod
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
    with patch("web.routers.setup.httpx.AsyncClient") as MockClient:
        client = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        MockClient.return_value = client
        success, msg = await mod._test_mimo("fake_key")
    assert success is True
    assert "成功" in msg


@pytest.mark.asyncio
async def test_test_mimo_http_error():
    """MiMo 非 200 → (False, HTTP 状态消息)。"""
    from web.routers import setup as mod
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    with patch("web.routers.setup.httpx.AsyncClient") as MockClient:
        client = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        MockClient.return_value = client
        success, msg = await mod._test_mimo("fake_key")
    assert success is False
    assert "401" in msg


@pytest.mark.asyncio
async def test_test_siliconflow_401():
    """SiliconFlow 401 → (False, 无效消息)。"""
    from web.routers import setup as mod
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    with patch("web.routers.setup.httpx.AsyncClient") as MockClient:
        client = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        MockClient.return_value = client
        success, msg = await mod._test_siliconflow("fake_key")
    assert success is False
    assert "无效" in msg


@pytest.mark.asyncio
async def test_test_timeout_mimo():
    """MiMo 超时 → (False, 超时消息)。"""
    import httpx
    from unittest.mock import AsyncMock
    from web.routers import setup as mod

    async def _timeout_post(*args, **kwargs):
        raise httpx.TimeoutException("timeout")

    with patch("web.routers.setup.httpx.AsyncClient") as MockClient:
        client = MagicMock()
        client.post = _timeout_post
        client.get = _timeout_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        MockClient.return_value = client
        success, msg = await mod._test_mimo("fake_key")
    assert success is False
    assert "超时" in msg


@pytest.mark.asyncio
async def test_test_qqbot_success():
    """QQ Bot 200 + access_token → (True, 成功)。"""
    from web.routers import setup as mod
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"access_token": "tok_xxx"}
    with patch("web.routers.setup.httpx.AsyncClient") as MockClient:
        client = MagicMock()
        client.post = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        MockClient.return_value = client
        success, msg = await mod._test_qqbot("app_id", "secret")
    assert success is True
    assert "成功" in msg


@pytest.mark.parametrize("fn_name,key_name", [
    ("_test_deepseek", "DeepSeek"),
    ("_test_openrouter", "OpenRouter"),
    ("_test_agnes", "Agnes"),
    ("_test_modelscope", "ModelScope"),
    ("_test_github", "GitHub"),
])
@pytest.mark.asyncio
async def test_get_200_success(fn_name, key_name):
    """GET 系 provider 200 → (True, 成功消息)。"""
    from web.routers import setup as mod
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("web.routers.setup.httpx.AsyncClient") as MockClient:
        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock()
        MockClient.return_value = client
        fn = getattr(mod, fn_name)
        success, msg = await fn("fake_key")
    assert success is True
    assert key_name in msg
