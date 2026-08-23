# tests/test_setup_anysearch_key.py — AnySearch Key 进 setup 向导选填清单 + 探针
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def test_optional_keys_contains_anysearch():
    """主清单（setup_wizard.OPTIONAL_KEYS）含 ANYSEARCH_API_KEY 且字段齐全。"""
    from setup_wizard import OPTIONAL_KEYS
    entry = next((k for k in OPTIONAL_KEYS if k["key"] == "ANYSEARCH_API_KEY"), None)
    assert entry is not None
    assert entry["label"] and entry["desc"] and entry["url"]
    # ALL_KEYS 联动（save_keys 写 .env 的依据）
    from setup_wizard import ALL_KEYS
    assert any(k["key"] == "ANYSEARCH_API_KEY" for k in ALL_KEYS)


def test_fallback_meta_contains_anysearch():
    """setup.py 降级清单同样包含（import 失败时的兜底渲染）。"""
    from web.routers import setup as mod
    entry = next((k for k in mod._FALLBACK_OPTIONAL_KEYS_META
                  if k["key"] == "ANYSEARCH_API_KEY"), None)
    assert entry is not None and entry["label"]


def _mock_client(resp):
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_probe_success_envelope():
    """200 + code:0 → 验证成功。"""
    from web.routers import setup as mod
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"code": 0, "message": "success", "data": {"results": []}}
    with patch("web.routers.setup_key_probes.httpx.AsyncClient") as MockClient:
        MockClient.return_value = _mock_client(resp)
        success, msg = await mod._test_anysearch("as_sk_x")
    assert success is True and "成功" in msg


@pytest.mark.asyncio
async def test_probe_auth_rejected():
    """401 → Key 无效（不降级匿名语义）。"""
    from web.routers import setup as mod
    resp = MagicMock()
    resp.status_code = 401
    with patch("web.routers.setup_key_probes.httpx.AsyncClient") as MockClient:
        MockClient.return_value = _mock_client(resp)
        success, msg = await mod._test_anysearch("bad")
    assert success is False and "无效" in msg


@pytest.mark.asyncio
async def test_probe_business_error():
    """200 但 code:-1 → 业务错误消息带 error_code。"""
    from web.routers import setup as mod
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"code": -1, "error_code": "quota_exceeded", "message": "额度用尽"}
    with patch("web.routers.setup_key_probes.httpx.AsyncClient") as MockClient:
        MockClient.return_value = _mock_client(resp)
        success, msg = await mod._test_anysearch("as_sk_x")
    assert success is False and "quota_exceeded" in msg


@pytest.mark.asyncio
async def test_dispatch_by_name(monkeypatch):
    """_test_key_by_name 分发到 _test_anysearch。"""
    from web.routers import setup_key_probes as probes
    called = {}

    async def _fake(key_value):
        called["v"] = key_value
        return True, "ok"

    monkeypatch.setattr(probes, "_test_anysearch", _fake)
    success, _ = await probes._test_key_by_name("ANYSEARCH_API_KEY", "as_sk_x", {})
    assert success is True and called["v"] == "as_sk_x"
