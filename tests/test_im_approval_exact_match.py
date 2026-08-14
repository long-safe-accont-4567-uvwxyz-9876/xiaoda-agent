"""IM 审批通道确认词整词匹配回归测试.

修复背景：`_classify_reply` 曾用子串包含匹配（`kw in lower`），
导致回复 "why"/"eyes"/"body" 这类包含 "y" 的任意文本被误判为 APPROVED，
放行高危操作。本测试断言确认词必须整词匹配。
"""
import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from security.human_approval import ApprovalStatus, IMApprovalChannel


def _make_channel() -> IMApprovalChannel:
    async def send_cb(text: str) -> None:
        pass
    return IMApprovalChannel(send_callback=send_cb, timeout=5.0)


@pytest.mark.parametrize("text", [
    "确认",
    "确定",
    "yes",
    "y",
    "Y",
    " y ",
    "YES",
    "  yes  ",
])
def test_confirm_texts_are_approved(text):
    """确认词（含首尾空白与大小写变体）应判定为 APPROVED。"""
    ch = _make_channel()
    assert ch._classify_reply(text) == ApprovalStatus.APPROVED


@pytest.mark.parametrize("text", [
    "why",
    "eyes",
    "body",
    "no",
    "n",
    "NO",
    "取消",
    "拒绝",
])
def test_non_confirm_texts_are_not_approved(text):
    """含 "y"/"n" 子串的任意文本不应被误判为 APPROVED。"""
    ch = _make_channel()
    assert ch._classify_reply(text) != ApprovalStatus.APPROVED


@pytest.mark.parametrize("text", [
    "取消",
    "拒绝",
    "no",
    "n",
    "NO",
])
def test_reject_texts_are_rejected(text):
    """拒绝词应判定为 REJECTED，确认优先于拒绝的顺序不受影响。"""
    ch = _make_channel()
    assert ch._classify_reply(text) == ApprovalStatus.REJECTED


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "why",
    "eyes",
    "body",
    "今天天气怎么样",
])
def test_unmatched_texts_return_none(text):
    """无关回复（含空白）应返回 None。"""
    ch = _make_channel()
    assert ch._classify_reply(text) is None
