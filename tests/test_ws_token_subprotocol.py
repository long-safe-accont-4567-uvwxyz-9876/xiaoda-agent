"""回归测试：WebSocket 鉴权 token 仅通过 Sec-WebSocket-Protocol 子协议传递（VULN-07）。

修复前：websocket_endpoint 签名带 token: str = ""，FastAPI 会从 URL query ?token=
自动解析，函数内 `token = subprotocol_token or token` 保留了 query 兜底，存在
URL query 传 token 的泄露风险。修复后：签名移除 query token 参数，仅从
Sec-WebSocket-Protocol 读取 token。
"""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from web.ws_hub import websocket_endpoint


def _mock_ws(subprotocol=None):
    """构造带 headers 的 mock WebSocket，模拟 Sec-WebSocket-Protocol 子协议。"""
    ws = AsyncMock()
    ws.headers = SimpleNamespace(get=lambda key: subprotocol)
    return ws


def test_endpoint_signature_removed_query_token_param():
    """函数签名不再接受 query token 参数（FastAPI 不再从 ?token= 解析）。"""
    sig = inspect.signature(websocket_endpoint)
    assert "token" not in sig.parameters, f"不应保留 query token 参数: {sig}"


async def test_query_only_token_closes_with_4001():
    """仅 URL query 携带 token（无子协议）：必须被拒绝，close code 4001。"""
    ws = _mock_ws(subprotocol=None)
    # 模拟 URL query 携带 token，但函数签名已无 query token 参数，无从读取
    ws.query_params = {"token": "valid-token"}
    with patch("web.routers.auth._validate_token", return_value=True):
        await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


async def test_subprotocol_valid_token_accepts_with_subprotocol():
    """子协议携带有效 token：accept 时回传该子协议。"""
    ws = _mock_ws(subprotocol="valid-token")
    with patch("web.routers.auth._validate_token", return_value=True), \
            patch("web.ws_hub.manager.register", side_effect=ValueError):
        await websocket_endpoint(ws)
    ws.accept.assert_awaited_once_with(subprotocol="valid-token")


async def test_subprotocol_invalid_token_closes_with_4001():
    """子协议携带无效 token：close 4001，不 accept。"""
    ws = _mock_ws(subprotocol="bad-token")
    with patch("web.routers.auth._validate_token", return_value=False):
        await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()
