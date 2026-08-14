"""前端契约：WsClient 用 WebSocket 子协议传递 token，URL 不再拼 ?token=。"""
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _source() -> str:
    return (ROOT / "web/frontend/src/api/ws.ts").read_text(encoding="utf-8")


def test_ws_uses_subprotocol_for_token():
    ws = _source()
    # token 通过 WebSocket 子协议数组传递
    assert "new WebSocket(this.url, [token])" in ws


def test_ws_url_does_not_append_token_query_param():
    ws = _source()
    # URL 不再拼接 ?token= 查询参数
    assert "${this.url}?token=" not in ws
    assert "?token=${token}" not in ws
