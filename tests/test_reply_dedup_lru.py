"""回复去重缓存的 LRU 淘汰测试。

根因：_recent_replies 原为 dict 类变量，session_id 含日期(SES-YYYYMMDD-...)，
每天产生新 key 且永不清理 → 长期运行内存泄漏 → 渐进退化。
修复：改 OrderedDict + LRU，超过 REPLY_DEDUP_SESSION_CAP 淘汰最久未访问的 key。

2026-08-05 架构变更：去重 key 从 session_id 改为 user_id。
  - 微信 adapter 不传 session_id（空串）→ 原 key 退化为 user_openid
  - QQ c2c 的 session_id 每小时换一次 → key 频繁失效 → 去重失效
  - 改用 user_id（wechat_{openid} / qq_{openid}），跨 session 稳定
LRU 维护不变：OrderedDict + move_to_end + popitem(last=False) 上限淘汰。

本测试聚焦 LRU 淘汰行为，用 user_openid 作为去重 key；
REPLY_DEDUP_THRESHOLD 调高到 99.0 避免触发重试路径（重试语义由其他测试覆盖）。
"""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_processor(cap: int) -> "MessageProcessorMixin":
    """构造仅持有 LRU 所需属性的裸 mixin。"""
    from agent_core.message_processor import MessageProcessorMixin

    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor._recent_replies = OrderedDict()
    processor.REPLY_DEDUP_MAX = 5
    # 99.0 阈值让所有回复都不重复 → 走 not-recent 直录分支，专注 LRU 结构
    processor.REPLY_DEDUP_THRESHOLD = 99.0
    processor.REPLY_DEDUP_RETRY_TIMEOUT = 15
    processor.REPLY_DEDUP_SESSION_CAP = cap
    processor.router = SimpleNamespace(route=AsyncMock())
    # 本测试仅验证内存 LRU 缓存，无需 DB 去重路径（self.db 为 None 时跳过）
    processor.db = None
    return processor


@pytest.mark.asyncio
async def test_recent_replies_evicts_oldest_sessions_beyond_cap():
    """超过 REPLY_DEDUP_SESSION_CAP 时，最旧 user 被 LRU 淘汰，防止内存泄漏。"""
    from agent_core.message_processor import MessageProcessorMixin

    cap = 8
    processor = _make_processor(cap)

    # 模拟 cap+5 个不同 user（每个 user 第一次访问，走 not recent 分支）
    for i in range(cap + 5):
        await MessageProcessorMixin._dedup_reply_against_recent(
            processor, reply=f"reply-{i}", messages=[], task_type="chat",
            _model_cfg={}, _cb_max_tokens=100, user_openid=f"wechat_u{i}",
            session_id="", trace=MagicMock(),
        )

    assert len(processor._recent_replies) <= cap, (
        f"user 数 {len(processor._recent_replies)} 超过 cap {cap}，内存泄漏"
    )
    # 最旧的 user 应被淘汰
    assert "wechat_u0" not in processor._recent_replies
    # 最新的应保留
    assert f"wechat_u{cap + 4}" in processor._recent_replies


@pytest.mark.asyncio
async def test_recent_replies_lru_promotes_recently_accessed_session():
    """访问旧 user 会将其提升到最近，避免被淘汰（LRU 正确性）。"""
    from agent_core.message_processor import MessageProcessorMixin

    cap = 4
    processor = _make_processor(cap)

    # 填满 cap
    for i in range(cap):
        await MessageProcessorMixin._dedup_reply_against_recent(
            processor, reply=f"reply-{i}", messages=[], task_type="chat",
            _model_cfg={}, _cb_max_tokens=100, user_openid=f"u{i}",
            session_id="", trace=MagicMock(),
        )
    # 重新访问最旧的 u0（提升到最近）
    await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply="reply-0-again", messages=[], task_type="chat",
        _model_cfg={}, _cb_max_tokens=100, user_openid="u0",
        session_id="", trace=MagicMock(),
    )
    # 再加 1 个新 user，应淘汰次旧（u1），而非被提升过的 u0
    await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply="reply-new", messages=[], task_type="chat",
        _model_cfg={}, _cb_max_tokens=100, user_openid="u-new",
        session_id="", trace=MagicMock(),
    )

    assert "u0" in processor._recent_replies, "被重新访问的 u0 不应被淘汰"
    assert "u1" not in processor._recent_replies, "次旧的 u1 应被淘汰"
