"""qq_bot_adapter 共享纯函数单元测试。"""
from __future__ import annotations

from unittest.mock import patch

from qq_bot_adapter import _parse_master_ids, _build_user_input


def test_parse_master_ids_comma_separated():
    with patch.dict("os.environ", {"MASTER_QQ_OPENID": "a,b , c"}):
        assert _parse_master_ids() == ["a", "b", "c"]


def test_parse_master_ids_empty():
    with patch.dict("os.environ", {"MASTER_QQ_OPENID": ""}):
        assert _parse_master_ids() == []


def test_build_user_input_with_content():
    assert _build_user_input("看这张图", "[图片: x.png]") == "看这张图 [图片: x.png]"


def test_build_user_input_attachment_only():
    assert _build_user_input("", "[图片: x.png]") == "[图片: x.png]"
