"""回归测试：CLI WSClient 通过 subprotocols 传 token，URL 不再拼 ?token=。

修复前：ws_url() 把 token 拼进 URL 查询串，WSClient.connect() 直接连该 URL。
修复后：ws_url() 返回不含 ?token= 的纯 URL，WSClient.connect() 用
websockets.connect(url, subprotocols=[token]) 传递 token。
"""
import asyncio

import cli_client


def test_ws_url_does_not_append_token_query():
    """ws_url 返回的 URL 不含 ?token= 查询串（token 改由 subprotocol 传递）。"""
    url = cli_client.ws_url(host="127.0.0.1", port=8080)
    assert "?token=" not in url, f"URL 不应包含 token 查询串: {url}"
    assert url == "ws://127.0.0.1:8080/ws"


def test_ws_client_connect_passes_token_as_subprotocol():
    """WSClient.connect 应把 token 作为 subprotocols 传给 websockets.connect。"""
    client = cli_client.WSClient(token="abc", host="127.0.0.1", port=8080)

    captured = {}

    async def _fake_connect(url, *args, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        captured["args"] = args
        return object()

    client._websockets.connect = _fake_connect

    asyncio.run(client.connect())

    assert captured["url"] == "ws://127.0.0.1:8080/ws"
    assert "?token=" not in captured["url"]
    assert captured["kwargs"]["subprotocols"] == ["abc"]
