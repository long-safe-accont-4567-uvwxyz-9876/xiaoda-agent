"""回归测试：WS 鉴权 token 不得包含 base64 '=' 填充字符。

根因：_issue_token 用 base64.urlsafe_b64encode 编码含浮点字符串的 payload，
约 2/3 概率产生 '=' / '==' 填充。'=' 不是合法 WebSocket 子协议字符，
前端 new WebSocket(url, [token]) 抛 SyntaxError，CLI websockets.connect
(subprotocols=[token]) 拒绝，完全阻断 WS 鉴权。

修复：签发端 rstrip('=')，验证端解码前补回 padding。
"""
import base64
import hashlib
import hmac
import secrets

from web.routers import auth


def _setup_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_revoked_path", lambda: tmp_path / "revoked.json")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    # 固定时钟：expiry = 0.0 + 7*86400 = 604800.0，str 长度 8（mod 3 = 2），
    # 签发必然产生 '=' 填充；同时 604800.0 > 0.0 保证 token 未过期。
    monkeypatch.setattr(auth.time, "time", lambda: 0.0)
    auth._tokens.clear()
    auth._revoked_cache.clear()
    auth._revoked_cache_mtime = 0.0
    grace = getattr(auth, "_revoked_grace", None)
    if grace is not None:
        grace.clear()


def _make_padded_token() -> tuple[str, float]:
    """构造一个确定含 '=' 填充的 token，返回 (token, expiry)。"""
    expiry = 604800.0  # 与 _setup_auth 固定时钟一致（str 长度 8，mod 3 = 2）
    nonce = secrets.token_hex(8)
    epoch = auth._load_token_epoch()
    payload = f"{expiry}.{nonce}.{epoch}"
    sig = hmac.new(auth._SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()
    assert "=" in token, "测试前提：构造的 token 应含 padding"
    return token, expiry


def test_issue_token_has_no_base64_padding(monkeypatch, tmp_path):
    """签发端返回的 token 不得含 '='（否则 WS 子协议非法）。"""
    _setup_auth(monkeypatch, tmp_path)

    for _ in range(5):
        token, _ = auth._issue_token()
        assert "=" not in token, f"token 不应含 base64 填充字符 '=': {token!r}"


def test_validate_token_accepts_unpadded_token(monkeypatch, tmp_path):
    """验证端对「去掉 padding 的 token」仍应返回 True。"""
    _setup_auth(monkeypatch, tmp_path)

    padded, _ = _make_padded_token()
    unpadded = padded.rstrip("=")

    assert auth._validate_token(unpadded) is True


def test_extract_expiry_accepts_unpadded_token(monkeypatch, tmp_path):
    """_extract_expiry 对「去掉 padding 的 token」应能提取过期时间。"""
    _setup_auth(monkeypatch, tmp_path)

    padded, expiry = _make_padded_token()
    unpadded = padded.rstrip("=")

    assert auth._extract_expiry(unpadded) == expiry
