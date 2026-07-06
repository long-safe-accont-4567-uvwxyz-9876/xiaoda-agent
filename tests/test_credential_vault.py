"""测试 security/credential_vault.py — API Key 加密存储

覆盖场景：
- 加解密往返
- 明文向后兼容（passthrough）
- is_encrypted 识别
- .env 迁移幂等性
- 机器绑定（不同用户名/主机名产生不同密文）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from security.credential_vault import (
    DecryptionError,
    decrypt,
    encrypt,
    is_encrypted,
    migrate_env_file,
)


def test_encrypt_decrypt_roundtrip():
    """加密后解密应等于原文"""
    plaintext = "sk-test-api-key-12345-ABCDE"
    encrypted = encrypt(plaintext)

    assert encrypted != plaintext
    assert is_encrypted(encrypted)
    assert decrypt(encrypted) == plaintext


def test_decrypt_plaintext_passthrough():
    """明文直接返回（向后兼容）"""
    assert decrypt("sk-plaintext-key") == "sk-plaintext-key"
    assert decrypt("") == ""
    assert decrypt("any-non-enc-value") == "any-non-enc-value"
    assert decrypt("MIMO_API_KEY=foo") == "MIMO_API_KEY=foo"


def test_is_encrypted():
    """正确识别加密/明文"""
    # 合法加密格式
    assert is_encrypted("enc:v1:YWJjZA==") is True
    # 明文与空值
    assert is_encrypted("") is False
    assert is_encrypted("plaintext") is False
    assert is_encrypted("sk-foo-bar") is False
    # 缺少 base64 主体
    assert is_encrypted("enc:v1:") is False
    # 加密产物必然是 enc:v1: 前缀
    assert is_encrypted(encrypt("test-secret")) is True


def test_migrate_env_idempotent(tmp_path):
    """重复迁移幂等"""
    env_file = tmp_path / ".env"
    original = (
        "MIMO_API_KEY=sk-plaintext-key\n"
        'AGNES_API_KEY="sk-agnes-key"\n'
        "WEBUI_PORT=8082\n"
        "# 注释行\n"
        "MIMO_BASE_URL=https://api.example.com/v1\n"
        "GITHUB_PERSONAL_ACCESS_TOKEN=ghp_token_abc\n"
    )
    env_file.write_text(original, encoding="utf-8")

    # 第一次迁移：MIMO_API_KEY + AGNES_API_KEY + GITHUB_PERSONAL_ACCESS_TOKEN
    n1 = migrate_env_file(str(env_file))
    assert n1 == 3

    content1 = env_file.read_text(encoding="utf-8")
    # 明文已被加密
    assert "enc:v1:" in content1
    assert "sk-plaintext-key" not in content1
    assert "sk-agnes-key" not in content1
    assert "ghp_token_abc" not in content1
    # 非敏感配置保持不变
    assert "WEBUI_PORT=8082" in content1
    assert "MIMO_BASE_URL=https://api.example.com/v1" in content1
    assert "# 注释行" in content1

    # 第二次迁移（幂等，不应重复加密）
    n2 = migrate_env_file(str(env_file))
    assert n2 == 0

    content2 = env_file.read_text(encoding="utf-8")
    assert content2 == content1


def test_encrypt_different_per_machine():
    """不同用户名/主机名产生不同密文（机器绑定，mock 验证）"""
    plaintext = "sk-same-secret-value"

    # 本机加密并解密
    enc_default = encrypt(plaintext)
    assert decrypt(enc_default) == plaintext

    # 模拟另一台机器（不同用户名 + 主机名）
    with patch("security.credential_vault.getpass") as mock_getpass, \
         patch("security.credential_vault.socket") as mock_socket:
        mock_getpass.getuser.return_value = "attacker"
        mock_socket.gethostname.return_value = "attacker-pc"
        enc_attacker = encrypt(plaintext)

    # 不同机器产生不同密文
    assert enc_attacker != enc_default
    # 本机无法解密攻击者机器的密文（HMAC 验证失败）—— 修复后抛 DecryptionError
    with pytest.raises(DecryptionError, match="HMAC 标签验证失败"):
        decrypt(enc_attacker)
    # 攻击者机器也无法解密本机的密文
    with patch("security.credential_vault.getpass") as mock_getpass, \
         patch("security.credential_vault.socket") as mock_socket:
        mock_getpass.getuser.return_value = "attacker"
        mock_socket.gethostname.return_value = "attacker-pc"
    with pytest.raises(DecryptionError, match="HMAC 标签验证失败"):
        decrypt(enc_default)
