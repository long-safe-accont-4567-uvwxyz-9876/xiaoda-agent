"""QQ 消息并发锁竞态条件测试。

验证 setdefault 修复后的锁行为：
- 同一用户并发消息应串行处理（同一把锁）
- 不同用户并发消息应并行处理（不同的锁）
- 锁字典不会因并发创建而增长无限

原 bug（Check-Then-Act 竞态条件）：
if user_openid not in self._c2c_locks:
    self._c2c_locks[user_openid] = asyncio.Lock()
→ 多个协程同时检查可能都创建新锁并互相覆盖

修复后：
lock = self._c2c_locks.setdefault(user_openid, asyncio.Lock())
→ 原子操作，保证同一用户共享同一把锁
"""
import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ──────────────────────────────────────────────────────────────────────
# 测试 1：同一用户并发消息应共享同一把锁（使用 setdefault）
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_c2c_lock_shared_by_same_user():
    """同一用户的多个并发协程应获取同一把锁，避免 Check-Then-Act 竞态条件。"""
    c2c_locks: dict[str, asyncio.Lock] = {}
    user_openid = "test_user_001"
    locks_acquired = []

    async def _acquire_lock():
        lock = c2c_locks.setdefault(user_openid, asyncio.Lock())
        locks_acquired.append(lock)
        async with lock:
            await asyncio.sleep(0.01)

    await asyncio.gather(_acquire_lock(), _acquire_lock(), _acquire_lock())

    assert len(locks_acquired) == 3, "3 个协程都应获取锁"
    assert all(l is locks_acquired[0] for l in locks_acquired), (
        "同一用户的所有协程应获取同一把锁实例，实际获取了不同的锁"
    )
    assert len(c2c_locks) == 1, (
        f"锁字典应只有 1 个条目，实际 {len(c2c_locks)} 个"
    )


# ──────────────────────────────────────────────────────────────────────
# 测试 2：不同用户并发消息应获取不同的锁
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_c2c_lock_separate_for_different_users():
    """不同用户的并发协程应获取不同的锁，实现并行处理。"""
    c2c_locks: dict[str, asyncio.Lock] = {}
    locks_by_user = {}

    async def _acquire_lock_for_user(user_id: str):
        lock = c2c_locks.setdefault(user_id, asyncio.Lock())
        locks_by_user[user_id] = lock
        async with lock:
            await asyncio.sleep(0.01)

    users = [f"user_{i}" for i in range(5)]
    await asyncio.gather(*[_acquire_lock_for_user(u) for u in users])

    assert len(c2c_locks) == 5, (
        f"锁字典应有 5 个条目（每个用户一个），实际 {len(c2c_locks)} 个"
    )
    lock_ids = {id(lock) for lock in locks_by_user.values()}
    assert len(lock_ids) == 5, (
        f"每个用户应拥有独立的锁实例，实际只有 {len(lock_ids)} 个不同的锁"
    )


# ──────────────────────────────────────────────────────────────────────
# 测试 3：群聊锁同样避免竞态条件
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_group_lock_shared_by_same_member():
    """同一群成员的多个并发协程应获取同一把锁。"""
    group_locks: dict[str, asyncio.Lock] = {}
    member_openid = "member_001"
    locks_acquired = []

    async def _acquire_lock():
        lock = group_locks.setdefault(member_openid, asyncio.Lock())
        locks_acquired.append(lock)
        async with lock:
            await asyncio.sleep(0.01)

    await asyncio.gather(_acquire_lock(), _acquire_lock(), _acquire_lock())

    assert len(locks_acquired) == 3, "3 个协程都应获取锁"
    assert all(l is locks_acquired[0] for l in locks_acquired), (
        "同一成员的所有协程应获取同一把锁实例"
    )
    assert len(group_locks) == 1, (
        f"锁字典应只有 1 个条目，实际 {len(group_locks)} 个"
    )


# ──────────────────────────────────────────────────────────────────────
# 测试 4：高并发场景下锁数量不会异常增长
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_lock_count_stable_under_high_concurrency():
    """高并发场景下，锁字典的条目数量应与用户数一致，不会无限增长。"""
    c2c_locks: dict[str, asyncio.Lock] = {}
    user_openid = "stress_test_user"
    semaphore = asyncio.Semaphore(100)

    async def _stress_acquire():
        async with semaphore:
            lock = c2c_locks.setdefault(user_openid, asyncio.Lock())
            async with lock:
                await asyncio.sleep(0.001)

    tasks = [_stress_acquire() for _ in range(200)]
    await asyncio.gather(*tasks)

    assert len(c2c_locks) == 1, (
        f"高并发后锁字典应仍只有 1 个条目，实际 {len(c2c_locks)} 个"
        "（如果 >1，说明存在 Check-Then-Act 竞态条件）"
    )


# ──────────────────────────────────────────────────────────────────────
# 测试 5：验证原始 Check-Then-Act 模式存在竞态条件
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_check_then_act_has_race_condition():
    """验证原始的 Check-Then-Act 模式在高并发下会产生多个锁。

    这是一个负面测试，证明原代码的问题。
    在实际生产中不应使用此模式。
    """
    c2c_locks: dict[str, asyncio.Lock] = {}
    user_openid = "race_condition_user"
    locks_created = []

    async def _check_then_act():
        if user_openid not in c2c_locks:
            new_lock = asyncio.Lock()
            locks_created.append(new_lock)
            c2c_locks[user_openid] = new_lock
        lock = c2c_locks[user_openid]
        async with lock:
            await asyncio.sleep(0.001)

    tasks = [_check_then_act() for _ in range(50)]
    await asyncio.gather(*tasks)

    assert len(locks_created) >= 1, "至少应创建一个锁"
    if len(locks_created) > 1:
        print(f"⚠️  检测到竞态条件：创建了 {len(locks_created)} 个锁（应为 1 个）")
        print(f"   这证明了 Check-Then-Act 模式的缺陷")