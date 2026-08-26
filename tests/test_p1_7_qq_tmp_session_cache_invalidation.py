"""P1-7: 临时 session qq_tmp_ 永不失效 bug 修复测试。

原 bug:
- qq_bot_adapter.py:_get_or_create_c2c_session 在 DB 超时/异常时返回
  f"qq_tmp_{user_openid[:16]}" 作为兜底 session_id。
- 该临时 ID 不存在于 sessions 表，但被写入 _c2c_session_cache。
- 后续消息命中缓存直接返回 qq_tmp_，DB UPDATE 零行生效不报错，
  所有消息都写到不存在的 session，上下文永久丢失。
- 失效判定仅 ValueError：qq_bot_adapter.py:774，不会纠正此状态。

修复:
- 缓存命中检查中检测 sid.startswith("qq_tmp_") → 视为缓存失效，
  跳过缓存继续查 DB，让后续消息能恢复到真实 session。

测试:
1. 缓存中是 qq_tmp_ 时，下次调用应跳过缓存重新查 DB
2. 缓存中是正常 sid 时，下次调用应命中缓存
3. DB 恢复后应返回真实 session_id 并更新缓存
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_bot():
    """构造一个最小可测的 AIQQBot 实例（跳过真实 botpy.Client 初始化）。"""
    from qq_bot_adapter import AIQQBot
    bot = AIQQBot.__new__(AIQQBot)
    # 手动设置必要属性
    bot._processed_msg_ids = {}
    bot._MSG_ID_TTL = 3600
    bot._last_c2c_openid = ""
    bot._c2c_session_cache = {}
    bot._c2c_session_cache_ttl = 3600
    bot._c2c_session_cache_ts = {}
    bot._C2C_SESSION_CACHE_MAX_SIZE = 1000
    bot._agent_shared = False
    bot._agent_initialized = False
    bot.agent = MagicMock()
    bot.nudge_engine = None
    bot.hitl_enabled = False
    bot.im_approval = MagicMock()
    bot._approval_message_ctx = None
    return bot


# ──────────────────────────────────────────────────────────────
# 测试 1：缓存中是 qq_tmp_ 时，下次调用应跳过缓存重新查 DB
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_qq_tmp_cached_sid_skips_cache_and_queries_db():
    """缓存命中 qq_tmp_ 开头的 sid 时应视为缓存失效，跳过缓存继续查 DB。

    P1-7 核心 bug：原版本缓存命中即返回，qq_tmp_ 被永久缓存，
    后续所有消息都写到不存在的 session。
    """
    bot = _make_bot()
    user_openid = "user_abc1234567890xyz"

    # 模拟缓存中已存在 qq_tmp_（之前 DB 超时降级写入的）
    bot._c2c_session_cache[user_openid] = f"qq_tmp_{user_openid[:16]}"
    bot._c2c_session_cache_ts[user_openid] = time.time()  # 未过期

    # mock agent.get_session 返回真实 session
    bot.agent.get_session = AsyncMock(return_value={"id": "real_sid_xyz"})

    sid = await bot._get_or_create_c2c_session(user_openid)

    # 验证：应跳过缓存，调用 DB 查询
    assert sid == "real_sid_xyz", (
        f"缓存中是 qq_tmp_ 时应跳过缓存查 DB 返回真实 sid，实际 {sid}"
    )
    # 验证：get_session 被调用（说明跳过了缓存）
    bot.agent.get_session.assert_called_once_with(user_openid)
    # 验证：缓存被更新为真实 sid
    assert bot._c2c_session_cache[user_openid] == "real_sid_xyz", (
        "缓存应被更新为真实 sid，不再保留 qq_tmp_"
    )


# ──────────────────────────────────────────────────────────────
# 测试 2：缓存中是正常 sid 时，下次调用应命中缓存（不查 DB）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_normal_cached_sid_hits_cache_no_db_query():
    """缓存命中正常 sid（非 qq_tmp_）时应直接返回，不查 DB。"""
    bot = _make_bot()
    user_openid = "user_abc1234567890xyz"
    real_sid = "real_session_id_123"

    # 模拟缓存中已存在正常 sid
    bot._c2c_session_cache[user_openid] = real_sid
    bot._c2c_session_cache_ts[user_openid] = time.time()  # 未过期

    # mock agent.get_session（不应被调用）
    bot.agent.get_session = AsyncMock(return_value={"id": "should_not_be_returned"})

    sid = await bot._get_or_create_c2c_session(user_openid)

    # 验证：应命中缓存，直接返回缓存的 sid
    assert sid == real_sid, (
        f"缓存命中正常 sid 时应直接返回，实际 {sid}"
    )
    # 验证：get_session 未被调用（缓存命中）
    bot.agent.get_session.assert_not_called()


# ──────────────────────────────────────────────────────────────
# 测试 3：qq_tmp_ 缓存命中后 DB 仍超时，应再次返回 qq_tmp_（不缓存）
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_qq_tmp_skip_cache_but_db_still_timeout_returns_tmp_without_caching():
    """qq_tmp_ 跳过缓存后若 DB 仍超时，应返回 qq_tmp_ 但不污染缓存。

    场景：DB 长时间不可用，每次都降级返回 qq_tmp_。
    关键：缓存不应被 qq_tmp_ 污染，否则 DB 恢复后仍命中 qq_tmp_ 缓存。
    """
    bot = _make_bot()
    user_openid = "user_abc1234567890xyz"

    # 模拟缓存中已存在 qq_tmp_（之前降级写入）
    bot._c2c_session_cache[user_openid] = f"qq_tmp_{user_openid[:16]}"
    bot._c2c_session_cache_ts[user_openid] = time.time()

    # mock agent.get_session 抛 TimeoutError（DB 仍不可用）
    bot.agent.get_session = AsyncMock(side_effect=TimeoutError())

    sid = await bot._get_or_create_c2c_session(user_openid)

    # 验证：仍返回 qq_tmp_（保证消息不丢失）
    assert sid == f"qq_tmp_{user_openid[:16]}", (
        f"DB 仍超时应返回 qq_tmp_ 兜底，实际 {sid}"
    )
    # 验证：get_session 被调用（说明跳过了缓存）
    bot.agent.get_session.assert_called()


# ──────────────────────────────────────────────────────────────
# 测试 4：qq_tmp_ 缓存命中后 DB 恢复，应返回真实 sid 并更新缓存
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_qq_tmp_skip_cache_db_recovers_returns_real_sid():
    """qq_tmp_ 跳过缓存后 DB 恢复，应返回真实 sid 并更新缓存。

    场景：上次 DB 超时降级到 qq_tmp_，本次 DB 恢复，应能拿到真实 sid。
    关键：修复后下次消息能恢复到真实 session，不会永久卡在 qq_tmp_。
    """
    bot = _make_bot()
    user_openid = "user_abc1234567890xyz"
    tmp_sid = f"qq_tmp_{user_openid[:16]}"
    real_sid = "real_session_id_recovered"

    # 第 1 次调用：DB 超时降级返回 qq_tmp_（模拟历史降级场景）
    bot._c2c_session_cache[user_openid] = tmp_sid
    bot._c2c_session_cache_ts[user_openid] = time.time()

    # mock agent.get_session 返回真实 session（DB 已恢复）
    bot.agent.get_session = AsyncMock(return_value={"id": real_sid})

    # 第 2 次调用：应跳过 qq_tmp_ 缓存，查 DB 拿到真实 sid
    sid = await bot._get_or_create_c2c_session(user_openid)
    assert sid == real_sid, (
        f"DB 恢复后应返回真实 sid，实际 {sid}"
    )

    # 验证：缓存被更新为真实 sid（不再保留 qq_tmp_）
    assert bot._c2c_session_cache[user_openid] == real_sid, (
        "缓存应被更新为真实 sid，下次能命中缓存"
    )


# ──────────────────────────────────────────────────────────────
# 测试 5：连续多条消息场景：第 1 条降级，第 2 条恢复
# ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_multi_message_recovery_after_tmp_fallback():
    """模拟连续两条消息：第 1 条 DB 超时降级，第 2 条 DB 恢复。

    P1-7 关键场景：原版本第 2 条仍命中 qq_tmp_ 缓存，DB UPDATE 零行生效，
    上下文永久丢失。修复后第 2 条应跳过缓存查 DB，恢复真实 session。
    """
    bot = _make_bot()
    user_openid = "user_abc1234567890xyz"
    real_sid = "real_session_id_456"

    # 模拟历史降级：缓存中已有 qq_tmp_
    bot._c2c_session_cache[user_openid] = f"qq_tmp_{user_openid[:16]}"
    bot._c2c_session_cache_ts[user_openid] = time.time()

    # 第 2 条消息：DB 已恢复，应跳过 qq_tmp_ 缓存
    bot.agent.get_session = AsyncMock(return_value={"id": real_sid})

    sid2 = await bot._get_or_create_c2c_session(user_openid)

    # 验证：第 2 条消息拿到真实 sid（而非缓存的 qq_tmp_）
    assert sid2 == real_sid, (
        f"第 2 条消息应恢复真实 sid，实际 {sid2}（应为 {real_sid}）"
    )
    # 验证：DB 被查询（说明跳过了缓存）
    bot.agent.get_session.assert_called_once_with(user_openid)
    # 验证：缓存被更新为真实 sid
    assert bot._c2c_session_cache[user_openid] == real_sid


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
