"""测试 token 吊销文件的原子写入完整性。

验证场景：_revoke_token 写入吊销文件时使用原子操作，
确保即使写入过程中断，文件也不会损坏导致已吊销 token 失效。
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def revoked_dir(tmp_path):
    """创建临时凭证目录，用于存放吊销文件。"""
    return tmp_path


@pytest.fixture
def mock_get_revoked_path(revoked_dir):
    """Mock _get_revoked_path 返回临时目录下的路径。"""
    revoked_path = revoked_dir / "revoked_tokens.json"
    with patch("web.routers.auth._get_revoked_path", return_value=revoked_path):
        yield revoked_path


def _make_token(expiry: float, nonce: str = "abcd1234") -> str:
    """构造一个格式合法的测试 token（base64 编码的 expiry.nonce.sig）。"""
    import base64
    import hashlib
    import hmac
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(b"test_secret", payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


class TestRevokeTokenAtomicity:
    """测试 _revoke_token 使用原子写入保护吊销数据完整性。"""

    def test_revoke_creates_valid_json(self, mock_get_revoked_path):
        """吊销 token 后，文件应为合法 JSON。"""
        from web.routers.auth import _revoke_token

        future = 9999999999.0  # 远未来
        token = _make_token(future)
        _revoke_token(token)

        # 文件应存在且为合法 JSON
        assert mock_get_revoked_path.exists()
        data = json.loads(mock_get_revoked_path.read_text(encoding="utf-8"))
        assert "revoked" in data
        assert token in data["revoked"]

    def test_revoke_preserves_existing_entries(self, mock_get_revoked_path):
        """多次吊销不应丢失之前的记录。"""
        from web.routers.auth import _revoke_token

        future = 9999999999.0
        token1 = _make_token(future, "nonce1")
        token2 = _make_token(future, "nonce2")

        _revoke_token(token1)
        _revoke_token(token2)

        data = json.loads(mock_get_revoked_path.read_text(encoding="utf-8"))
        assert token1 in data["revoked"]
        assert token2 in data["revoked"]

    def test_no_partial_write_on_failure(self, mock_get_revoked_path):
        """如果原子写入的临时文件阶段失败，原文件不应损坏。"""
        from web.routers.auth import _revoke_token

        future = 9999999999.0
        token1 = _make_token(future, "nonce1")
        _revoke_token(token1)

        # 记录原始内容
        original_content = mock_get_revoked_path.read_text(encoding="utf-8")
        original_data = json.loads(original_content)

        # Mock atomic_write 使其抛出异常（模拟写入失败）
        with patch("web.routers.auth.atomic_write", side_effect=OSError("disk full")):
            token2 = _make_token(future, "nonce2")
            _revoke_token(token2)  # 应被 try/except 吞掉

        # 原文件应未被修改（atomic_write 失败不应破坏原文件）
        current_content = mock_get_revoked_path.read_text(encoding="utf-8")
        current_data = json.loads(current_content)
        assert current_data == original_data
        assert token2 not in current_data["revoked"]

    def test_is_revoked_after_revoke(self, mock_get_revoked_path):
        """吊销后的 token 应被 _is_revoked 正确识别。"""
        from web.routers.auth import _revoke_token, _is_revoked

        future = 9999999999.0
        token = _make_token(future, "revtest")
        _revoke_token(token)

        # 清除缓存强制重新读取文件
        import web.routers.auth as auth_mod
        auth_mod._revoked_cache_mtime = 0.0
        auth_mod._revoked_cache = set()

        assert _is_revoked(token) is True

    def test_concurrent_revoke_no_data_loss(self, mock_get_revoked_path):
        """快速连续吊销多个 token 不应丢失任何记录。"""
        from web.routers.auth import _revoke_token

        future = 9999999999.0
        tokens = [_make_token(future, f"concurrent_{i}") for i in range(10)]

        for token in tokens:
            _revoke_token(token)

        data = json.loads(mock_get_revoked_path.read_text(encoding="utf-8"))
        for token in tokens:
            assert token in data["revoked"], f"Token {token[:20]}... lost after sequential revoke"
