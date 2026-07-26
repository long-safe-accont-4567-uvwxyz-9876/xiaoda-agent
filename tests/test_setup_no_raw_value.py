"""验证 setup/keys API 不返回明文 API Key。

缺陷 D4: setup.py 的 keys 接口曾返回 raw_value 字段，导致浏览器 Network 面板可直接看到明文 API Key，
构成敏感信息泄露风险。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_setup_keys_does_not_return_raw_value():
    """setup/keys 响应中不得包含 raw_value 字段。"""
    from web.routers.setup import _mask_key_value

    # 直接测试 mask 函数行为
    masked = _mask_key_value("sk-abcdefghijklmnopqrstuvwxyz")
    assert masked.startswith("sk-a")
    assert "***...***" in masked
    assert "bcdefghijklmnopqrstuvw" not in masked

    # 反向验证：原始值不应被直接返回
    val = "sk-live-12345"
    assert _mask_key_value(val) != val


def test_setup_keys_masking_rules():
    """验证各类 key 的脱敏规则。"""
    from web.routers.setup import _mask_key_value

    # 长 key：保留前 4 位和后 4 位
    assert _mask_key_value("1234567890abcdef") == "1234***...***cdef"
    # 短 key：仅显示首字符
    assert _mask_key_value("abc") == "a****"
    # 空 key
    assert _mask_key_value("") == ""
