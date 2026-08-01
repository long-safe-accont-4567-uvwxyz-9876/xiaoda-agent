"""跨对话回复去重缓存的 LRU 淘汰测试。

根因：_recent_replies 原为 dict 类变量，session_id 含日期(SES-YYYYMMDD-...)，
每天产生新 key 且永不清理 → 长期运行内存泄漏 → 渐进退化。
修复：改 OrderedDict + LRU，超过 REPLY_DEDUP_SESSION_CAP 淘汰最久未访问的 session。
"""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_recent_replies_evicts_oldest_sessions_beyond_cap():
    """超过 REPLY_DEDUP_SESSION_CAP 时，最旧 session 被 LRU 淘汰，防止内存泄漏。"""
    from agent_core.message_processor import MessageProcessorMixin

    cap = 8
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor._recent_replies = OrderedDict()
    processor.REPLY_DEDUP_MAX = 5
    processor.REPLY_DEDUP_THRESHOLD = 75.0
    processor.REPLY_DEDUP_RETRY_TIMEOUT = 15
    # cap 在修复后才存在；测试隔离用独立 OrderedDict
    processor.REPLY_DEDUP_SESSION_CAP = cap
    processor.router = SimpleNamespace(route=AsyncMock())

    # 模拟 cap+5 个不同 session（每个 session 第一次访问，走 not recent 分支）
    for i in range(cap + 5):
        sess = f"SES-20260801-{i:05d}"
        await MessageProcessorMixin._dedup_reply_against_recent(
            processor, reply=f"reply-{i}", messages=[], task_type="chat",
            _model_cfg={}, _cb_max_tokens=100, user_openid="",
            session_id=sess, trace=MagicMock(),
        )

    assert len(processor._recent_replies) <= cap, (
        f"session 数 {len(processor._recent_replies)} 超过 cap {cap}，内存泄漏"
    )
    # 最旧的 session 应被淘汰
    assert "SES-20260801-00000" not in processor._recent_replies
    # 最新的应保留
    assert "SES-20260801-00012" in processor._recent_replies


@pytest.mark.asyncio
async def test_recent_replies_lru_promotes_recently_accessed_session():
    """访问旧 session 会将其提升到最近，避免被淘汰（LRU 正确性）。"""
    from agent_core.message_processor import MessageProcessorMixin

    cap = 4
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor._recent_replies = OrderedDict()
    processor.REPLY_DEDUP_MAX = 5
    processor.REPLY_DEDUP_THRESHOLD = 75.0
    processor.REPLY_DEDUP_RETRY_TIMEOUT = 15
    processor.REPLY_DEDUP_SESSION_CAP = cap
    processor.router = SimpleNamespace(route=AsyncMock())

    # 填满 cap
    for i in range(cap):
        await MessageProcessorMixin._dedup_reply_against_recent(
            processor, reply=f"reply-{i}", messages=[], task_type="chat",
            _model_cfg={}, _cb_max_tokens=100, user_openid="",
            session_id=f"SES-{i}", trace=MagicMock(),
        )
    # 重新访问最旧的 SES-0（提升到最近）
    await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply="reply-0-again", messages=[], task_type="chat",
        _model_cfg={}, _cb_max_tokens=100, user_openid="",
        session_id="SES-0", trace=MagicMock(),
    )
    # 再加 1 个新 session，应淘汰次旧（SES-1），而非被提升过的 SES-0
    await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply="reply-new", messages=[], task_type="chat",
        _model_cfg={}, _cb_max_tokens=100, user_openid="",
        session_id="SES-new", trace=MagicMock(),
    )

    assert "SES-0" in processor._recent_replies, "被重新访问的 SES-0 不应被淘汰"
    assert "SES-1" not in processor._recent_replies, "次旧的 SES-1 应被淘汰"
