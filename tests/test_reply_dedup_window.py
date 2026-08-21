"""回复去重窗口修复测试 —— 相同用户消息隔多轮重发必须触发重试。

根因（2026-08-21 生产日志铁证）：去重只跟"最近 1 条回复"对比
（recent[:1] + DB limit=1）。用户 09:51 与 11:53 两次发"看看"得到完全
相同的回复，dedup max_sim 仅 9.9（窗口里全是其他消息的回复）→ 去重失效。
修复：按交换对 (user_message, assistant_reply) 取最近窗口，新回复先用当前
用户消息做字面相似度匹配（>= REPLY_DEDUP_USER_SIM 视为同一问题重发），
命中交换对的旧回复作为对比候选；无匹配时退回"最近 1 条"语义。
"""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.message_processor import MessageProcessorMixin


def _make_processor(db_pairs=None):
    """构造带假 DB 的裸 mixin（db_pairs 为 (user_msg, reply) 列表，最新在前）。"""
    processor = MessageProcessorMixin.__new__(MessageProcessorMixin)
    processor._recent_replies = OrderedDict()
    processor.REPLY_DEDUP_MAX = 5
    processor.REPLY_DEDUP_THRESHOLD = 70.0
    processor.REPLY_DEDUP_RETRY_TIMEOUT = 15
    processor.REPLY_DEDUP_SESSION_CAP = 256
    processor.REPLY_DEDUP_USER_SIM = 95.0
    processor.REPLY_DEDUP_DB_WINDOW = 20
    processor.router = SimpleNamespace(route=AsyncMock())
    # 生产环境由 ToolExecutorMixin 经 MRO 提供；测试里给个最小实现
    processor._clean_reply = lambda text: text.strip()
    processor.db = None
    if db_pairs is not None:
        processor.db = SimpleNamespace(
            get_recent_exchanges=AsyncMock(return_value=db_pairs))
    return processor


@pytest.mark.asyncio
async def test_same_user_message_long_window_matches_and_retries_once():
    """相同问题隔若干轮再发：命中窗口里该问题历史回复并重试一次。

    复现生产案例：用户发"看看"时机隔 9 条其他消息，新回复与旧回复
    字面完全相同（100% 相似）→ 必须触发重试，且采用重试结果。
    """
    # DB 最新在前：第一条"在吗"是最新一轮，目标旧回复"看看"在前面的轮次
    processor = _make_processor(db_pairs=[
        ("在吗", "小妲在的"),
        ("看看", "那小妲就给你看一下哦"),
        ("不清", "再近一点点"),
        ("在吗", "在呀"),
        ("看看", "那小妲就给你看一下哦"),
    ])
    retry_text = "换个完全不同的角度说说今天看到的云彩和心情，小妲觉得今天的风特别舒服呢"
    processor.router.route = AsyncMock(return_value=retry_text)

    reply = "那小妲就给你看一下哦"  # 与 1 小时前的回复完全重复
    result = await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply=reply,
        messages=[{"role": "system", "content": "s"},
                  {"role": "user", "content": "看看"}],
        task_type="chat", _model_cfg={}, _cb_max_tokens=100,
        user_openid="qq_111", session_id="", trace=MagicMock(),
    )
    assert processor.db.get_recent_exchanges.await_count == 1
    assert processor.router.route.await_count == 1  # 触发一次重试
    assert result == retry_text
    buf = processor._recent_replies["qq_111"]
    assert buf and buf[-1] == ("看看", retry_text)


@pytest.mark.asyncio
async def test_different_user_message_falls_back_to_latest_reply():
    """用户消息不同（无相同问题匹配）：退回只跟最近 1 条对比。

    保持 08-05 语义：不同问题即使回复文风相似，只要与最近 1 条回复
    不重复就不触发重试，避免"在吗/在嘛"类变体消息反复触发第二次调用。
    """
    processor = _make_processor(db_pairs=[
        ("在吗", "在呢～"),
        ("吃什么", "刚刚吃什么"),
    ])
    reply = "刚刚吃什么"  # 与较旧条重复，但与最新 1 条（"在呢～"）不同
    result = await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply=reply,
        messages=[{"role": "system", "content": "s"},
                  {"role": "user", "content": "刚聊的什么"}],
        task_type="chat", _model_cfg={}, _cb_max_tokens=100,
        user_openid="qq_222", session_id="", trace=MagicMock(),
    )
    assert processor.router.route.await_count == 0  # 未触发重试
    assert result == reply
    assert processor._recent_replies["qq_222"][-1] == ("刚聊的什么", reply)


@pytest.mark.asyncio
async def test_fallback_latest_reply_still_triggers_dedupe_when_duplicated():
    """退化路径（无用户消息文本，messages=None）仍按最近 1 条回复去重。"""
    processor = _make_processor(db_pairs=[
        ("在吗", "在呢，小妲一直在呢～"),
        ("看看", "那好吧，看完要还哦！"),
    ])
    retry_text = "完全不同的一句话，换个说法重新讲给你听，这样总可以吧"
    processor.router.route = AsyncMock(return_value=retry_text)
    dup_reply = "在呢，小妲一直在呢～"  # 与最近一条回复完全重复
    result = await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply=dup_reply, messages=None,
        task_type="chat", _model_cfg={}, _cb_max_tokens=100,
        user_openid="u3", session_id="", trace=MagicMock(),
    )
    assert result == retry_text


@pytest.mark.asyncio
async def test_retry_still_duplicate_picks_lower_similarity():
    """重试后仍相似：取相似度较低版本返回，且只重试一次。"""
    processor = _make_processor(db_pairs=[
        ("原问题", "小妲今天超开心的！"),
    ])
    retry_text = "小妲今天可开心了，心情像开花一样灿烂，各种小事情都顺顺利利"  # 与旧回复仍高相似（>70%）
    processor.router.route = AsyncMock(return_value=retry_text)
    reply = "小妲今天超开心的！"
    result = await MessageProcessorMixin._dedup_reply_against_recent(
        processor, reply=reply,
        messages=[{"role": "system", "content": "s"},
                  {"role": "user", "content": "原问题"}],
        task_type="chat", _model_cfg={}, _cb_max_tokens=100,
        user_openid="u4", session_id="", trace=MagicMock(),
    )
    assert processor.router.route.await_count == 1  # 只重试一次
    assert result == retry_text  # 相似度取低者兜底


def test_extract_user_text_handles_multimodal_blocks():
    """_extract_user_text 兼容多块（图+文字）消息结构。"""
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": [
            {"type": "image", "text": ""},
            {"type": "text", "text": "帮我看看这张图"},
        ]},
    ]
    assert MessageProcessorMixin._extract_user_text(messages) == "帮我看看这张图"
    messages2 = [{"role": "user", "content": "  在吗  "}]
    assert MessageProcessorMixin._extract_user_text(messages2) == "在吗"
    assert MessageProcessorMixin._extract_user_text([]) == ""