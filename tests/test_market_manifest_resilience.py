"""回归测试：市场清单拉取遇到非法 id 时应跳过该 item，而不是整份清单返回 None。

根因：MarketItem.id 加了 field_validator（正则 ^[a-zA-Z0-9][a-zA-Z0-9_-]*$），
远端返回一条含非法字符的 id 就会抛 ValidationError，被 _fetch_modelscope /
_fetch_mcp_hub 外层 except 吞掉，导致整份清单返回 None（可用性 DoS）。

修复：循环内对每个 MarketItem(...) 做 per-item 容错，跳过非法 item。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market import manifest


def _modelscope_body(skills: list[dict]) -> dict:
    return {"success": True, "data": {"skills": skills, "total": len(skills)}}


def _skill(skill_id: str) -> dict:
    return {
        "id": skill_id,
        "display_name": skill_id,
        "description": "",
        "developer": "",
        "tags": [],
        "logo_url": "",
        "source_url": "",
        "license": "",
        "downloads": 0,
    }


def _mcp_hub_body(servers: list[dict]) -> dict:
    return {"code": 0, "data": servers, "pagination": {"total": len(servers)}}


def _server(server_id: str) -> dict:
    return {
        "server_id": server_id,
        "qualified_name": server_id,
        "display_name": server_id,
        "description": "",
        "creator": "",
        "tag": [],
        "logo": "",
        "use_count": 0,
        "connections": "",
    }


def _make_client(resp_body: dict):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=resp_body)

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_modelscope_skips_invalid_id_items():
    skills = [_skill("legit-one"), _skill("bad/id"), _skill("legit_two")]
    client = _make_client(_modelscope_body(skills))

    fetcher = manifest.ManifestFetcher(source="modelscope", item_type="plugin")
    with patch("httpx.AsyncClient", return_value=client):
        result = await fetcher._fetch_modelscope()

    assert result is not None, "单个脏 id 不应导致整份清单返回 None"
    ids = [item.id for item in result.items]
    assert "plugin-legit-one" in ids
    assert "plugin-legit_two" in ids
    # 非法 id 被规范化保留（bad/id → bad-id），而不是导致整份清单失败
    assert "plugin-bad-id" in ids
    assert "plugin-bad/id" not in ids
    assert len(result.items) == 3


@pytest.mark.asyncio
async def test_mcp_hub_skips_invalid_id_items():
    servers = [_server("legit-a"), _server("bad id!"), _server("legit-b")]
    client = _make_client(_mcp_hub_body(servers))

    fetcher = manifest.ManifestFetcher(source="mcp_hub", item_type="mcp")
    with patch("httpx.AsyncClient", return_value=client):
        result = await fetcher._fetch_mcp_hub()

    assert result is not None, "单个脏 id 不应导致整份清单返回 None"
    ids = [item.id for item in result.items]
    assert "mcp-legit-a" in ids
    assert "mcp-legit-b" in ids
    assert len(result.items) == 2
