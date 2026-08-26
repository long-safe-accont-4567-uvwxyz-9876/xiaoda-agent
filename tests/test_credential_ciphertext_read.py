"""凭证读取口径统一测试 —— 密文场景不再把 enc:v1: 密文当 Key。

覆盖（回归）：
- _resolve_provider_key 对 enc:v1: 密文自动解密，返回明文（旧实现直读 os.getenv 会拿密文当 Key → 401）
- _resolve_provider_key 对明文值原样返回（向后兼容）
- _resolve_provider_key 对缺失/空值返回空串
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import security.credential_vault as cv
from model_router import _resolve_provider_key
from security.credential_vault import encrypt

PLAIN = "sk-test-mimo-key-1234567890"


@pytest.fixture(autouse=True)
def _clean_env():
    """隔离 MIMO_API_KEY，测试间互不污染。"""
    old = os.environ.pop("MIMO_API_KEY", None)
    yield
    if old is None:
        os.environ.pop("MIMO_API_KEY", None)
    else:
        os.environ["MIMO_API_KEY"] = old


def test_resolve_provider_key_decrypts_ciphertext():
    """密文场景：env 里是 enc:v1: 密文时，应返回明文，而非密文。"""
    with patch.object(cv, "HAS_WIN32CRYPT", False):
        enc = encrypt(PLAIN)
    assert enc.startswith("enc:v1:")
    os.environ["MIMO_API_KEY"] = enc
    assert _resolve_provider_key("MIMO_API_KEY") == PLAIN


def test_resolve_provider_key_plaintext_unchanged():
    """明文场景：无 enc: 前缀的明文应原样返回（无回归）。"""
    os.environ["MIMO_API_KEY"] = PLAIN
    assert _resolve_provider_key("MIMO_API_KEY") == PLAIN


def test_resolve_provider_key_missing_returns_empty():
    """缺失场景：未配置时返回空串。"""
    os.environ.pop("MIMO_API_KEY", None)
    assert _resolve_provider_key("MIMO_API_KEY") == ""
