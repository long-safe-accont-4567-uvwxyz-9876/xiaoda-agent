"""验证 MCPClient 在 HTTP/SSE 传输模式下能正确发送请求。

缺陷 D2: _request() 方法曾未检查 self._http_client，导致 SSE/HTTP 传输时始终 fallback 到 stdio，
_stdio 分支因 process 未启动而直接返回 None，所有 HTTP 模式调用均失效。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_request_routes_to_http_when_http_client_exists():
    """当 _http_client 不为 None 时，_request 应路由到 _request_http。"""
    from tool_engine.mcp_client import MCPClient

    client = MCPClient("test_server")
    client._http_client = MagicMock()
    client._config = MagicMock(url="http://localhost:8080")

    with patch.object(client, "_request_http", new_callable=AsyncMock) as mock_http:
        mock_http.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        result = await client._request({"method": "tools/list"}, timeout=10.0)

    mock_http.assert_awaited_once()
    assert result == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_request_http_returns_none_when_no_http_or_stdio():
    """当 _http_client 为 None 且 stdio process 未启动时，_request 应返回 None。"""
    from tool_engine.mcp_client import MCPClient

    client = MCPClient("test_server")
    client._http_client = None
    client._process = None

    result = await client._request({"method": "tools/list"})
    assert result is None


@pytest.mark.asyncio
async def test_request_http_sends_post_with_json_rpc():
    """_request_http 应通过 HTTP POST 发送 JSON-RPC 请求并解析响应。"""
    from tool_engine.mcp_client import MCPClient

    client = MCPClient("test_server")
    client._config = MagicMock(url="http://localhost:8080")
    client._session_id = "sess_123"
    client._next_id = 1

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')

    mock_http_client = MagicMock()
    mock_http_client.stream = MagicMock()
    mock_http_client.stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_http_client.stream.return_value.__aexit__ = AsyncMock(return_value=False)

    client._http_client = mock_http_client

    result = await client._request_http({"method": "tools/list"}, timeout=10.0)

    # _request_http 返回 data.get("result")，即 JSON-RPC 响应的 result 字段
    assert result == {"tools": []}
    # 验证 POST 参数（stream 方法签名为 stream(method, url, **kwargs)）
    args, kwargs = mock_http_client.stream.call_args
    assert args[0] == "POST"
    assert kwargs["json"]["method"] == "tools/list"
    assert kwargs["headers"]["Mcp-Session-Id"] == "sess_123"
