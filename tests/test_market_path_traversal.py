"""TDD 测试：市场条目 id 校验，防止路径穿越。

Issue: MarketItem.id 未校验，恶意市场 API 返回 ../xxx 可在安装时
将插件/技能/MCP 配置文件写到任意目录。

修复方案：给 MarketItem.id 加正则校验（^[a-zA-Z0-9][a-zA-Z0-9_-]*$），
禁止 ..、/、\\ 以及以 . 开头的 id。
"""

import pytest
from pydantic import ValidationError


@pytest.mark.parametrize("bad_id", [
    "../evil",
    "..\\evil",
    "a/b",
    "/abs/path",
    ".hidden",
    "a..b",
    "..",
    "",
])
def test_market_item_rejects_path_traversal_ids(bad_id: str):
    from market.manifest import MarketItem

    with pytest.raises(ValidationError):
        MarketItem(id=bad_id, type="plugin", name="evil")


@pytest.mark.parametrize("good_id", [
    "my-plugin",
    "abc_123",
    "Plugin1",
    "skill-mcp_x",
    "a",
])
def test_market_item_accepts_legitimate_ids(good_id: str):
    from market.manifest import MarketItem

    item = MarketItem(id=good_id, type="skill", name="legit")
    assert item.id == good_id


def test_market_manifest_rejects_items_with_traversal_ids():
    from market.manifest import MarketManifest

    with pytest.raises(ValidationError):
        MarketManifest(items=[
            {"id": "../evil", "type": "plugin", "name": "evil"},
        ])
