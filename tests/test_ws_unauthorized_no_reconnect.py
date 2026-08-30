"""修复：无效 token 时 WS 端点以 4001 关闭，前端据此停止重连、清 token 跳登录。

背景：服务端对无效 token 直接 close(1008)，而前端 onclose 只识别 4001 / UNAUTHORIZED
消息停止重连，导致 1008 落入 scheduleReconnect 无限重连。约定：未授权 close code 统一
为 4001（4000-4999 为应用私有段，前端已识别 4001 = token 失效）。
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from web.ws_hub import websocket_endpoint

ROOT = Path(__file__).parents[1]


def _method_source(source_text: str, signature: str) -> str:
    """提取从 signature 开始的完整平衡花括号块（用于前端源码契约断言）。"""
    start = source_text.index(signature)
    body_start = source_text.index("{", start)
    depth = 0
    for index in range(body_start, len(source_text)):
        if source_text[index] == "{":
            depth += 1
        elif source_text[index] == "}":
            depth -= 1
            if depth == 0:
                return source_text[start:index + 1]
    raise ValueError(f"Unclosed body: {signature}")


def _mock_ws(subprotocol=None):
    """构造带 headers 的 mock WebSocket，用于模拟 Sec-WebSocket-Protocol 子协议。"""
    ws = AsyncMock()
    ws.headers = SimpleNamespace(get=lambda key: subprotocol)
    return ws


async def test_invalid_token_closes_with_4001():
    """无效 token：close code 必须为 4001（前端据此停止重连），且不 accept。"""
    ws = _mock_ws(subprotocol="invalid-token")
    with patch("web.routers.auth._validate_token", return_value=False):
        await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


async def test_missing_token_closes_with_4001():
    """缺失 token：同样以 4001 关闭，不 accept。"""
    ws = _mock_ws()
    await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


async def test_subprotocol_valid_token_accepts_with_subprotocol():
    """子协议携带有效 token：accept 时必须回传该子协议。"""
    ws = _mock_ws(subprotocol="valid-token")
    with patch("web.routers.auth._validate_token", return_value=True), \
            patch("web.ws_hub.manager.register", side_effect=ValueError):
        await websocket_endpoint(ws)
    ws.accept.assert_awaited_once_with(subprotocol="valid-token")


async def test_subprotocol_invalid_token_closes_with_4001():
    """子协议携带无效 token：以 4001 关闭，且不 accept。"""
    ws = _mock_ws(subprotocol="bad-token")
    with patch("web.routers.auth._validate_token", return_value=False):
        await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


async def test_query_token_rejected_closes_with_4001():
    """弃用 query token：无子协议时即使 validate 通过也以 4001 拒绝，不 accept。"""
    ws = _mock_ws()
    with patch("web.routers.auth._validate_token", return_value=True):
        await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


async def test_no_token_without_subprotocol_closes_with_4001():
    """既无子协议也无 query token：以 4001 关闭。"""
    ws = _mock_ws()
    await websocket_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4001, reason="Unauthorized")
    ws.accept.assert_not_awaited()


def test_ws_onclose_4001_stops_reconnect_and_clears_token():
    """前端契约：onclose 对 4001 停止重连，并清 token 跳登录。"""
    ws_src = (ROOT / "web/frontend/src/api/ws.ts").read_text(encoding="utf-8")
    # T10 身份守卫重构后 handler 绑定在局部 socket 变量上（旧签名为
    # this.ws.onclose），契约语义（4001 → 停止重连 + 清 token 跳登录）未变。
    onclose = _method_source(ws_src, "socket.onclose = (event) => {")
    assert "event.code === 4001" in onclose
    # 4001 分支必须在 scheduleReconnect 之前（即走"不重连"分支）
    assert onclose.index("event.code === 4001") < onclose.index("scheduleReconnect()")
    branch = onclose[
        onclose.index("event.code === 4001"):onclose.index("scheduleReconnect()")
    ]
    assert "this._handleUnauthorized()" in branch
    helper = _method_source(ws_src, "private _handleUnauthorized()")
    assert "localStorage.removeItem('token')" in helper
    assert "localStorage.removeItem('expires_at')" in helper
    assert "location.hash = '#/login'" in helper
