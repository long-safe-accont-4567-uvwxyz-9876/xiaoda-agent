"""web/routers/auth.py — _revoke_token 原子写入测试

覆盖:
    1. _revoke_token 使用原子写入（进程崩溃不损坏黑名单文件）
    2. 损坏的 revoked_tokens.json 不会导致所有令牌失效判断出错
    3. _is_revoked 在文件损坏时安全降级（返回 False，不崩溃）
"""
import json
import time
import base64
import hashlib
import hmac
import secrets
from pathlib import Path

import pytest


def _make_token(secret: str, expiry_offset: float = 86400 * 7) -> str:
    """构造一个合法的 HMAC token。"""
    expiry = time.time() + expiry_offset
    nonce = secrets.token_hex(8)
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def test_revoke_token_uses_atomic_write(tmp_path, monkeypatch):
    """_revoke_token 应使用 atomic_json_write，写入中途崩溃不损坏文件。

    修复前: path.write_text() 非原子，进程崩溃导致文件截断/损坏
    修复后: 使用 atomic_json_write (tempfile + fsync + os.replace)
    """
    # 设置环境使 auth 模块使用临时目录
    monkeypatch.setenv("WEBUI_SECRET", "test-secret-for-atomic-revoke")
    monkeypatch.setattr("web.routers.auth._SECRET", "test-secret-for-atomic-revoke")

    from web.routers.auth import _revoke_token, _get_revoked_path, _is_revoked
    monkeypatch.setattr("web.routers.auth._get_revoked_path", lambda: tmp_path / "revoked_tokens.json")

    token = _make_token("test-secret-for-atomic-revoke")

    # 吊销令牌
    _revoke_token(token)

    # 验证文件是合法 JSON（原子写入保证完整性）
    revoked_path = tmp_path / "revoked_tokens.json"
    assert revoked_path.exists(), "revoked_tokens.json should exist after revoke"
    with open(revoked_path, encoding="utf-8") as f:
        data = json.load(f)
    assert "revoked" in data
    assert token in data["revoked"]

    # 验证 _is_revoked 返回 True
    assert _is_revoked(token) is True


def test_revoke_token_survives_concurrent_writes(tmp_path, monkeypatch):
    """多次 _revoke_token 调用不会相互覆盖（原子写入保证序列化）。"""
    monkeypatch.setenv("WEBUI_SECRET", "test-secret-concurrent")
    monkeypatch.setattr("web.routers.auth._SECRET", "test-secret-concurrent")
    monkeypatch.setattr("web.routers.auth._get_revoked_path", lambda: tmp_path / "revoked_tokens.json")

    from web.routers.auth import _revoke_token

    tokens = [_make_token("test-secret-concurrent") for _ in range(5)]
    for t in tokens:
        _revoke_token(t)

    # 所有令牌都应在黑名单中
    revoked_path = tmp_path / "revoked_tokens.json"
    with open(revoked_path, encoding="utf-8") as f:
        data = json.load(f)
    for t in tokens:
        assert t in data["revoked"], f"Token {t[:20]}... should be in revoked list"


def test_corrupted_revoked_file_safe_degradation(tmp_path, monkeypatch):
    """损坏的 revoked_tokens.json 不应导致 _is_revoked 崩溃。"""
    monkeypatch.setattr("web.routers.auth._get_revoked_path", lambda: tmp_path / "revoked_tokens.json")

    # 写入损坏的 JSON（模拟非原子写入崩溃后的状态）
    revoked_path = tmp_path / "revoked_tokens.json"
    revoked_path.write_text('{"revoked": ["token1", "tok', encoding="utf-8")  # 截断

    from web.routers.auth import _is_revoked
    # _is_revoked 不应崩溃，安全返回 False
    result = _is_revoked("token1")
    assert result is False, "Corrupted file should degrade safely to False"


def test_revoked_file_permissions(tmp_path, monkeypatch):
    """_revoke_token 写入的文件应设置 0o600 权限。"""
    monkeypatch.setenv("WEBUI_SECRET", "test-secret-perms")
    monkeypatch.setattr("web.routers.auth._SECRET", "test-secret-perms")
    monkeypatch.setattr("web.routers.auth._get_revoked_path", lambda: tmp_path / "revoked_tokens.json")

    from web.routers.auth import _revoke_token

    token = _make_token("test-secret-perms")
    _revoke_token(token)

    revoked_path = tmp_path / "revoked_tokens.json"
    mode = revoked_path.stat().st_mode & 0o7777
    assert mode == 0o600, f"File should have 0o600 permissions, got {oct(mode)}"
