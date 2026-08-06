"""凭证读取链路测试 —— web/_provider_keys._decode_key 与 utils/env_reader.read_env_key

覆盖（回归）：
- _decode_key 对 enc:v1: 往返解密（官方包路径）
- _decode_key 对 enc:v2:dpapi: 前缀解密（Windows + pywin32 环境写入，
  此前不识别导致 load_provider_key 返回空 → provider 未注册 → 降级）
- _decode_key 在无 pywin32 时对 DPAPI 密文返回 None（不可解密，符合预期）
- read_env_key 对 .env 中 enc: 密文自动解密（此前直读密文当 Key → 401 → 降级）
- read_env_key 对明文值保持原样
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import security.credential_vault as cv
from security.credential_vault import encrypt
from utils.env_reader import read_env_key
from web._provider_keys import _decode_key

PLAIN = "sk-test-provider-key-1234567890"


def test_decode_key_v1_roundtrip():
    """官方包路径：enc:v1: 密文应能解回明文。"""
    enc = encrypt(PLAIN)
    assert enc.startswith("enc:v1:")
    assert _decode_key(enc) == PLAIN


def test_decode_key_dpapi_prefix():
    """Windows + pywin32 环境写入的 enc:v2:dpapi: 密文应能解回明文。"""
    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt:
        mock_win32crypt.CryptProtectData.return_value = b"dpapi-blob"
        enc = encrypt(PLAIN)
        assert enc.startswith("enc:v2:dpapi:")

        mock_win32crypt.CryptUnprotectData.return_value = (
            None, PLAIN.encode("utf-8"), None,
        )
        assert _decode_key(enc) == PLAIN


def test_decode_key_dpapi_unavailable_returns_none():
    """无 pywin32 时 DPAPI 密文无法解密，返回 None（凭证不可识别）。"""
    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt:
        mock_win32crypt.CryptProtectData.return_value = b"dpapi-blob"
        enc = encrypt(PLAIN)
    # 无 pywin32：decrypt 的 DPAPI 分支不可用，payload 非 v1 格式 → 解密失败
    with patch.object(cv, "HAS_WIN32CRYPT", False):
        assert _decode_key(enc) is None


def test_read_env_key_decrypts_encrypted_value():
    """read_env_key 应解密 enc: 前缀密文，而非直读密文当 Key。"""
    enc = encrypt(PLAIN)
    tmp_env = Path(__file__).parent / "tmp_env_reader_test.env"
    tmp_env.write_text(f"MIMO_API_KEY={enc}\n", encoding="utf-8")
    try:
        with patch("config.ENV_PATH", str(tmp_env)), \
             patch.dict(os.environ, {"MIMO_API_KEY": ""}, clear=False):
            # 强制走 .env 文件读取分支（os.environ 中为空）
            assert read_env_key("MIMO_API_KEY") == PLAIN
    finally:
        tmp_env.unlink(missing_ok=True)


def test_read_env_key_plaintext_unchanged():
    """read_env_key 对明文值应原样返回（向后兼容）。"""
    tmp_env = Path(__file__).parent / "tmp_env_reader_test.env"
    tmp_env.write_text(f"MIMO_API_KEY={PLAIN}\n", encoding="utf-8")
    try:
        with patch("config.ENV_PATH", str(tmp_env)), \
             patch.dict(os.environ, {"MIMO_API_KEY": ""}, clear=False):
            assert read_env_key("MIMO_API_KEY") == PLAIN
    finally:
        tmp_env.unlink(missing_ok=True)
