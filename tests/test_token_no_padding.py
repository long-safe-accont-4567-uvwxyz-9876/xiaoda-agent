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
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import auth

_BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


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


def _make_block_aligned_token() -> tuple[str, float]:
    """构造编码长度为 4 的倍数的合法无 padding token。"""
    nonce = secrets.token_hex(8)
    epoch = auth._load_token_epoch()
    for precision in range(10):
        expiry_str = f"{time.time() + 86400:.{precision}f}"
        payload = f"{expiry_str}.{nonce}.{epoch}"
        sig = hmac.new(auth._SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode().rstrip("=")
        if len(token) % 4 == 0:
            return token, float(expiry_str)
    raise AssertionError("无法构造编码长度为 4 的倍数的 token")


def _insert_invalid_character(token: str) -> str:
    midpoint = len(token) // 2
    return f"{token[:midpoint]}!{token[midpoint:]}"


def _append_padding(token: str) -> str:
    return f"{token}="


def _change_unused_pad_bits(token: str) -> str:
    """修改未使用的尾部位，使解码字节相同但文本表示不规范。"""
    remainder = len(token) % 4
    assert remainder in (2, 3), "测试前提：token 尾部必须存在未使用位"
    index = _BASE64URL_ALPHABET.index(token[-1])
    pad_bit_variants = 16 if remainder == 2 else 4
    assert index % pad_bit_variants == 0
    return f"{token[:-1]}{_BASE64URL_ALPHABET[index + 1]}"


@pytest.fixture
def auth_client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_SECRET", "test-secret")
    monkeypatch.setattr(auth, "_get_revoked_path", lambda: tmp_path / "revoked.json")
    monkeypatch.setattr(auth, "_get_token_epoch_path", lambda: tmp_path / "epoch")
    monkeypatch.setattr(auth, "_token_epoch", None)
    auth._tokens.clear()
    auth._revoked_cache.clear()
    auth._revoked_cache_mtime = 0.0
    auth._revoked_grace.clear()

    app = FastAPI()
    app.include_router(auth.router)
    with TestClient(app) as client:
        yield client


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


def test_validate_and_extract_reject_noncanonical_base64url(monkeypatch, tmp_path):
    _setup_auth(monkeypatch, tmp_path)
    block_aligned, _ = _make_block_aligned_token()
    padded, _ = _make_padded_token()
    canonical_with_unused_bits = padded.rstrip("=")

    malformed_tokens = (
        _insert_invalid_character(block_aligned),
        _append_padding(block_aligned),
        _change_unused_pad_bits(canonical_with_unused_bits),
        "A",
    )

    for token in malformed_tokens:
        assert auth._validate_token(token) is False
        assert auth._extract_expiry(token) == 0.0


@pytest.mark.parametrize(
    "mutate",
    (_insert_invalid_character, _append_padding),
    ids=("insert-invalid-character", "append-padding"),
)
def test_logout_invalidates_noncanonical_token_variants(auth_client, mutate):
    token, _ = _make_block_aligned_token()

    logout_response = auth_client.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logout_response.status_code == 200

    variant_response = auth_client.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {mutate(token)}"},
    )
    assert variant_response.status_code == 401
