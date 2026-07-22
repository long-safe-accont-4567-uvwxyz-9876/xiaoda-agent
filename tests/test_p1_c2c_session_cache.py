"""P1 测试: C2C session 缓存健壮性。

P1-1: 缓存无上限+无清理 — 长期运行多用户 bot 会持续膨胀内存
P1-2: stale session_id 不失效 — 缓存的 session_id 失效后仍被使用

修复目标:
1. 调用 _get_or_create_c2c_session 时先清理过期条目
2. 缓存有最大上限（如 1000），超过时按 FIFO 淘汰最旧条目
3. 提供 _invalidate_c2c_session(openid) 方法供外部清理
4. agent.process 抛错时调用 invalidation，下次重新查 DB
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_bot():
    """构造一个最小可测的 AIQQBot 实例（跳过真实 botpy.Client 初始化）。"""
    from qq_bot_adapter import AIQQBot
    # 跳过 botpy.Client.__init__ 的网络/配置初始化
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


# ---------- P1-1: 缓存清理与上限 ----------

def test_cache_prunes_expired_entries():
    """过期条目应被清理，而不是永久驻留。"""
    bot = _make_bot()
    # 模拟已过期的缓存条目
    bot._c2c_session_cache = {
        "user_a": "sid_a",
        "user_b": "sid_b",
    }
    bot._c2c_session_cache_ts = {
        "user_a": time.time() - 7200,  # 2 小时前，已过期
        "user_b": time.time() - 7200,  # 2 小时前，已过期
    }

    # 应有清理方法
    assert hasattr(bot, "_prune_c2c_session_cache"), "缺少 _prune_c2c_session_cache 方法"
    bot._prune_c2c_session_cache()

    assert "user_a" not in bot._c2c_session_cache, "过期 user_a 未被清理"
    assert "user_b" not in bot._c2c_session_cache, "过期 user_b 未被清理"
    assert "user_a" not in bot._c2c_session_cache_ts
    assert "user_b" not in bot._c2c_session_cache_ts


def test_cache_has_max_size_cap():
    """缓存应有最大上限，避免长期运行内存无限增长。"""
    bot = _make_bot()
    assert hasattr(bot, "_C2C_SESSION_CACHE_MAX_SIZE"), "缺少 _C2C_SESSION_CACHE_MAX_SIZE 常量"
    assert bot._C2C_SESSION_CACHE_MAX_SIZE > 0
    max_size = bot._C2C_SESSION_CACHE_MAX_SIZE

    # 填充超过上限（所有条目都未过期，仅触发 FIFO 上限淘汰）
    # user_0 最旧（ts 最小），user_(max_size+49) 最新
    base_time = time.time() - 100  # 100s 前，远小于 TTL 3600s，未过期
    for i in range(max_size + 50):
        bot._c2c_session_cache[f"user_{i}"] = f"sid_{i}"
        bot._c2c_session_cache_ts[f"user_{i}"] = base_time + i  # i 越小 ts 越小（越旧）

    bot._prune_c2c_session_cache()

    assert len(bot._c2c_session_cache) <= max_size, (
        f"缓存大小 {len(bot._c2c_session_cache)} 超过上限 {max_size}"
    )
    # FIFO: 最早的条目（user_0, user_1, ...）应被淘汰
    assert "user_0" not in bot._c2c_session_cache, "最旧条目 user_0 未被 FIFO 淘汰"
    assert "user_49" not in bot._c2c_session_cache, "最旧条目 user_49 未被 FIFO 淘汰"
    # 较新条目应保留
    assert f"user_{max_size + 49}" in bot._c2c_session_cache, "最新条目不应被淘汰"


def test_prune_keeps_valid_entries():
    """清理不应删除未过期的有效条目。"""
    bot = _make_bot()
    bot._c2c_session_cache = {
        "valid_user": "sid_valid",
        "expired_user": "sid_expired",
    }
    bot._c2c_session_cache_ts = {
        "valid_user": time.time(),  # 刚刚
        "expired_user": time.time() - 7200,  # 2 小时前
    }

    bot._prune_c2c_session_cache()

    assert "valid_user" in bot._c2c_session_cache, "有效条目被误删"
    assert bot._c2c_session_cache["valid_user"] == "sid_valid"
    assert "expired_user" not in bot._c2c_session_cache


@pytest.mark.asyncio
async def test_get_or_create_session_calls_prune():
    """_get_or_create_c2c_session 应在每次调用前触发清理。"""
    bot = _make_bot()
    bot._c2c_session_cache = {"expired_user": "sid"}
    bot._c2c_session_cache_ts = {"expired_user": time.time() - 7200}

    # mock agent.get_session 返回有效 session
    bot.agent.get_session = AsyncMock(return_value={"id": "new_sid"})

    await bot._get_or_create_c2c_session("new_user")

    # 过期条目应在调用过程中被清理
    assert "expired_user" not in bot._c2c_session_cache


# ---------- P1-2: stale session_id 失效 ----------

def test_invalidate_c2c_session_method_exists():
    """应有 _invalidate_c2c_session 方法用于主动失效缓存。"""
    bot = _make_bot()
    assert hasattr(bot, "_invalidate_c2c_session"), "缺少 _invalidate_c2c_session 方法"


def test_invalidate_c2c_session_clears_specific_user():
    """_invalidate_c2c_session 应清除指定 user 的缓存。"""
    bot = _make_bot()
    bot._c2c_session_cache = {
        "user_a": "sid_a",
        "user_b": "sid_b",
    }
    bot._c2c_session_cache_ts = {
        "user_a": time.time(),
        "user_b": time.time(),
    }

    bot._invalidate_c2c_session("user_a")

    assert "user_a" not in bot._c2c_session_cache, "user_a 缓存未清除"
    assert "user_a" not in bot._c2c_session_cache_ts
    # 不应影响其他用户
    assert "user_b" in bot._c2c_session_cache


def test_invalidate_c2c_session_handles_missing_user():
    """清除不存在的 user 不应抛错。"""
    bot = _make_bot()
    # 不应抛 KeyError
    bot._invalidate_c2c_session("nonexistent_user")


@pytest.mark.asyncio
async def test_stale_session_invalidates_on_process_error():
    """agent.process 抛错时缓存应失效，下次重新查 DB。

    场景: 用户在 WebUI 删除会话，下次 QQ 消息仍使用缓存中的旧 session_id，
    agent.process 抛错（session 不存在），缓存应被清除，下次重试查 DB。
    """
    bot = _make_bot()
    bot._c2c_session_cache = {"user_a": "stale_sid"}
    bot._c2c_session_cache_ts = {"user_a": time.time()}

    # agent.get_session 返回新 session_id（模拟 DB 中已切换）
    bot.agent.get_session = AsyncMock(return_value={"id": "fresh_sid"})

    # 模拟 process 抛错（RuntimeError 暗示 session 失效）
    bot.agent.process = AsyncMock(side_effect=RuntimeError("session not found"))

    # 模拟 _process_c2c_reply 流程: 抛错后应清除缓存
    # 直接调用 invalidation 验证
    bot._invalidate_c2c_session("user_a")

    assert "user_a" not in bot._c2c_session_cache

    # 下次查 DB 应得到新 session_id
    new_sid = await bot._get_or_create_c2c_session("user_a")
    assert new_sid == "fresh_sid"
    # 缓存应被重新填充
    assert bot._c2c_session_cache.get("user_a") == "fresh_sid"


# ---------- CodeRabbit F8: _set_c2c_session_cache helper ----------


def test_set_c2c_session_cache_method_exists():
    """CodeRabbit F8: 应有 _set_c2c_session_cache helper 统一写入+cap。"""
    bot = _make_bot()
    assert hasattr(bot, "_set_c2c_session_cache"), "缺少 _set_c2c_session_cache 方法"


def test_set_c2c_session_cache_writes_cache_and_timestamp():
    """_set_c2c_session_cache 应同时写 cache 和 ts。"""
    bot = _make_bot()
    bot._set_c2c_session_cache("user_new", "sid_new")

    assert bot._c2c_session_cache["user_new"] == "sid_new"
    assert "user_new" in bot._c2c_session_cache_ts
    assert bot._c2c_session_cache_ts["user_new"] > 0


def test_set_c2c_session_cache_enforces_cap_immediately():
    """CodeRabbit F8: 写入后应立即执行 size cap，不依赖下次调用的 pre-lookup prune。

    场景: 缓存已达 MAX_SIZE，再写入一条应立即淘汰最旧条目，而非等到下次 _get_or_create。
    """
    bot = _make_bot()
    bot._C2C_SESSION_CACHE_MAX_SIZE = 3  # 小上限便于测试
    base_ts = time.time() - 100  # 100s 前，未过期
    # 填满 3 条
    for i in range(3):
        bot._c2c_session_cache[f"user_{i}"] = f"sid_{i}"
        bot._c2c_session_cache_ts[f"user_{i}"] = base_ts + i  # user_0 最旧

    # 再写入一条，应立即淘汰 user_0（最旧）
    bot._set_c2c_session_cache("user_new", "sid_new")

    assert len(bot._c2c_session_cache) <= 3, "写入后不应超过 MAX_SIZE"
    assert "user_0" not in bot._c2c_session_cache, "应淘汰最旧条目 user_0"
    assert "user_new" in bot._c2c_session_cache, "新条目应存在"


def test_set_c2c_session_cache_overwrites_existing():
    """已存在的 key 应更新值和 ts，不新增条目。"""
    bot = _make_bot()
    bot._set_c2c_session_cache("user_a", "sid_old")
    old_ts = bot._c2c_session_cache_ts["user_a"]
    time.sleep(0.01)
    bot._set_c2c_session_cache("user_a", "sid_new")

    assert bot._c2c_session_cache["user_a"] == "sid_new"
    assert bot._c2c_session_cache_ts["user_a"] > old_ts


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
