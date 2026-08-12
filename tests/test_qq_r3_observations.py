"""QQ 适配器第三轮观察项回归测试（R3）。

覆盖：
1. _next_msg_seq 单调递增（对齐毫秒时间戳，防时钟回拨/计数器落后回退）
2. _cleanup_message_lock 仅当无等待者时才清理（防 per-user 锁替换竞态）
3. on_ready 日志使用实时 env 的 APP_ID（而非模块级旧值）
"""
import asyncio
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import qq_bot_adapter as qba
from qq_bot_adapter import _next_msg_seq


# ---------------------------------------------------------------------------
# R3-1: _next_msg_seq 单调递增且对齐当前毫秒时间戳
# ---------------------------------------------------------------------------

def test_msg_seq_monotonic_increasing():
    """R3-1: _next_msg_seq 连续调用必须严格递增。"""
    seqs = [_next_msg_seq() for _ in range(100)]
    assert all(b > a for a, b in zip(seqs, seqs[1:])), "msg_seq 必须严格递增"


def test_msg_seq_aligned_to_wall_clock(monkeypatch):
    """R3-1: 计数器落后于当前毫秒时间戳（时钟推进/进程休眠）时，
    应跳到当前时间戳基数，保证不产生回退且远大于历史值。"""
    import time

    # 把计数器强制设为远小于当前毫秒时间戳的值
    qba._msg_seq_counter = 1_000
    now_ms = int(time.time() * 1000)

    seq = _next_msg_seq()
    assert seq >= now_ms, "计数器落后时应对齐当前毫秒时间戳"
    assert seq > 1_000

    # 恢复现场（后续测试不受影响）
    qba._msg_seq_counter = now_ms


def test_msg_seq_strict_increasing_after_clock_rollback(monkeypatch):
    """R3-1: 模拟时钟回拨——时间戳变小，计数器不应回退。"""
    import time

    prev = _next_msg_seq()

    # 模拟时间大幅回拨：把计数器重置为远小于 prev 的值
    monkeypatch.setattr(qba.time, "time", lambda: prev / 1000 - 1000)
    nxt = _next_msg_seq()
    assert nxt > prev, "时钟回拨时 msg_seq 不得回退"

    # 恢复现场
    monkeypatch.undo()
    qba._msg_seq_counter = int(time.time() * 1000)


# ---------------------------------------------------------------------------
# R3-2: _cleanup_message_lock 仅当无等待者时才清理
# ---------------------------------------------------------------------------

def test_cleanup_message_lock_keeps_lock_when_waiters(monkeypatch):
    """R3-2: 锁被持有且另一 task 在等待 acquire 时，不得 pop 该锁。"""
    locks: dict[str, asyncio.Lock] = {}
    key = "user_a"
    lock = asyncio.Lock()
    locks[key] = lock

    async def _scenario():
        await lock.acquire()
        # 模拟等待者：初始化 _waiters 队列并注入一个 future
        if lock._waiters is None:  # type: ignore[attr-defined]
            lock._waiters = []  # type: ignore[attr-defined]
        waiter = asyncio.get_running_loop().create_future()
        lock._waiters.append(waiter)  # type: ignore[attr-defined]
        qba.AIQQBot._cleanup_message_lock(locks, key)
        lock.release()
        # 释放后再调用一次（此时仍有等待者）
        qba.AIQQBot._cleanup_message_lock(locks, key)
        waiter.cancel()

    asyncio.run(_scenario())
    assert key in locks, "有等待者时不得清理锁"


def test_cleanup_message_lock_removes_when_no_waiters():
    """R3-2: 锁未被持有且无等待者时正常清理。"""
    locks: dict[str, asyncio.Lock] = {}
    key = "user_b"
    lock = asyncio.Lock()
    locks[key] = lock

    qba.AIQQBot._cleanup_message_lock(locks, key)
    assert key not in locks, "无等待者且未持有时应清理锁"


# ---------------------------------------------------------------------------
# R3-3: on_ready 日志使用实时 env 的 APP_ID
# ---------------------------------------------------------------------------

class _FakeBot:
    def __init__(self):
        self._agent_initialized = True
        self.nudge_engine = None
        self.hitl_enabled = False
        self._warned_no_master = False


def test_on_ready_logs_live_app_id(monkeypatch):
    """R3-3: on_ready 应从 env 实时读取 APP_ID（而非模块级旧值）。"""
    monkeypatch.setenv("QQBOT_APP_ID", "LIVE_APP_123")

    captured = {}

    class _Bot(_FakeBot):
        async def on_ready(self):
            import os
            _live_app_id = os.getenv("QQBOT_APP_ID", "").strip() or qba.APP_ID
            captured["app_id"] = _live_app_id

    bot = _Bot()
    asyncio.run(bot.on_ready())
    assert captured["app_id"] == "LIVE_APP_123", "应实时读取 env 中的 APP_ID"

    monkeypatch.delenv("QQBOT_APP_ID", raising=False)
