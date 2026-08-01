"""P0-3: Self-Wake 挂起/恢复机制 — 测试

测试 core/self_wake.py 的 WakeTrigger、WakeRecord、SelfWakeManager。
"""
import asyncio
import time
import pytest

from core.self_wake import (
    WakeTrigger,
    WakeState,
    WakeRecord,
    SelfWakeManager,
    get_self_wake_manager,
)


class TestWakeTrigger:
    """WakeTrigger 枚举测试"""

    def test_trigger_values(self):
        assert WakeTrigger.TIMER == "timer"
        assert WakeTrigger.COMPLETION == "completion"
        assert WakeTrigger.EVENT == "event"

    def test_trigger_is_string_enum(self):
        assert isinstance(WakeTrigger.TIMER, str)


class TestWakeState:
    """WakeState 枚举测试"""

    def test_state_values(self):
        assert WakeState.PENDING == "pending"
        assert WakeState.DUE == "due"
        assert WakeState.FIRED == "fired"


class TestWakeRecord:
    """WakeRecord 数据类测试"""

    def test_default_values(self):
        async def cb():
            pass

        record = WakeRecord(
            id="test1",
            trigger=WakeTrigger.TIMER,
            callback=cb,
        )
        assert record.id == "test1"
        assert record.trigger == WakeTrigger.TIMER
        assert record.state == WakeState.PENDING
        assert record.fire_at == 0.0
        assert record.job_id == ""
        assert record.event_key == ""

    def test_is_expired_no_timeout(self):
        async def cb():
            pass
        record = WakeRecord(id="test", trigger=WakeTrigger.TIMER, callback=cb)
        assert not record.is_expired

    def test_is_expired_with_timeout(self):
        async def cb():
            pass
        record = WakeRecord(
            id="test", trigger=WakeTrigger.TIMER, callback=cb,
            timeout_at=time.time() - 1,  # 已过期
        )
        assert record.is_expired

    def test_is_due_pending_timer_not_yet(self):
        async def cb():
            pass
        record = WakeRecord(
            id="test", trigger=WakeTrigger.TIMER, callback=cb,
            fire_at=time.time() + 3600,  # 1小时后
        )
        assert not record.is_due

    def test_is_due_pending_timer_past(self):
        async def cb():
            pass
        record = WakeRecord(
            id="test", trigger=WakeTrigger.TIMER, callback=cb,
            fire_at=time.time() - 1,  # 已到期
        )
        assert record.is_due

    def test_is_due_fired_not_due(self):
        async def cb():
            pass
        record = WakeRecord(
            id="test", trigger=WakeTrigger.TIMER, callback=cb,
            fire_at=time.time() - 1,
        )
        record.state = WakeState.FIRED
        assert not record.is_due


class TestSelfWakeManager:
    """SelfWakeManager 测试"""

    @pytest.fixture
    def manager(self):
        return SelfWakeManager()

    @pytest.mark.asyncio
    async def test_register_timer(self, manager):
        """注册定时唤醒"""
        async def cb():
            pass

        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=60,
        )
        assert record.trigger == WakeTrigger.TIMER
        assert record.state == WakeState.PENDING
        assert record.fire_at > time.time()

    @pytest.mark.asyncio
    async def test_register_completion(self, manager):
        """注册任务完成唤醒"""
        async def cb():
            pass

        record = manager.register(
            trigger=WakeTrigger.COMPLETION,
            callback=cb,
            job_id="task_123",
        )
        assert record.trigger == WakeTrigger.COMPLETION
        assert record.job_id == "task_123"

    @pytest.mark.asyncio
    async def test_register_event(self, manager):
        """注册事件唤醒"""
        async def cb():
            pass

        record = manager.register(
            trigger=WakeTrigger.EVENT,
            callback=cb,
            event_key="user_message",
        )
        assert record.trigger == WakeTrigger.EVENT
        assert record.event_key == "user_message"

    @pytest.mark.asyncio
    async def test_check_due_timer(self, manager):
        """检查到期的 TIMER 唤醒"""
        called = False

        async def cb():
            nonlocal called
            called = True

        # 注册一个已过期的 TIMER
        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=-1,  # 立即到期
        )
        due = manager.check_due()
        assert len(due) == 1
        assert due[0].id == record.id

    @pytest.mark.asyncio
    async def test_check_due_not_yet(self, manager):
        """未到期的 TIMER 不在 due 列表"""
        async def cb():
            pass

        manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=3600,
        )
        due = manager.check_due()
        assert len(due) == 0

    @pytest.mark.asyncio
    async def test_fire_callback(self, manager):
        """触发唤醒回调"""
        called = False

        async def cb():
            nonlocal called
            called = True

        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=-1,
        )
        manager.check_due()
        result = await manager.fire(record.id)
        assert result
        assert called
        assert record.state == WakeState.FIRED

    @pytest.mark.asyncio
    async def test_fire_already_fired(self, manager):
        """重复触发已 fired 的记录返回 False"""
        async def cb():
            pass

        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=-1,
        )
        await manager.fire(record.id)
        result = await manager.fire(record.id)
        assert not result

    @pytest.mark.asyncio
    async def test_fire_nonexistent(self, manager):
        """触发不存在的记录返回 False"""
        result = await manager.fire("nonexistent")
        assert not result

    @pytest.mark.asyncio
    async def test_complete_job(self, manager):
        """任务完成触发 COMPLETION 唤醒"""
        async def cb():
            pass

        manager.register(
            trigger=WakeTrigger.COMPLETION,
            callback=cb,
            job_id="task_123",
        )
        fired = manager.complete_job("task_123")
        assert len(fired) == 1
        assert fired[0].state == WakeState.DUE

    @pytest.mark.asyncio
    async def test_complete_job_no_match(self, manager):
        """完成不存在的任务不触发任何唤醒"""
        async def cb():
            pass

        manager.register(
            trigger=WakeTrigger.COMPLETION,
            callback=cb,
            job_id="task_123",
        )
        fired = manager.complete_job("nonexistent")
        assert len(fired) == 0

    @pytest.mark.asyncio
    async def test_fire_event(self, manager):
        """事件触发 EVENT 唤醒"""
        async def cb():
            pass

        manager.register(
            trigger=WakeTrigger.EVENT,
            callback=cb,
            event_key="user_message",
        )
        fired = manager.fire_event("user_message")
        assert len(fired) == 1
        assert fired[0].state == WakeState.DUE

    @pytest.mark.asyncio
    async def test_fire_event_no_match(self, manager):
        """不匹配的事件不触发"""
        async def cb():
            pass

        manager.register(
            trigger=WakeTrigger.EVENT,
            callback=cb,
            event_key="user_message",
        )
        fired = manager.fire_event("other_event")
        assert len(fired) == 0

    @pytest.mark.asyncio
    async def test_cancel(self, manager):
        """取消唤醒记录"""
        async def cb():
            pass

        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=60,
        )
        assert manager.cancel(record.id)
        assert record.state == WakeState.FIRED

    @pytest.mark.asyncio
    async def test_cancel_already_fired(self, manager):
        """取消已 fired 的记录返回 False"""
        async def cb():
            pass

        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=cb,
            timeout_seconds=-1,
        )
        await manager.fire(record.id)
        assert not manager.cancel(record.id)

    @pytest.mark.asyncio
    async def test_pending(self, manager):
        """获取未触发的唤醒记录"""
        async def cb():
            pass

        r1 = manager.register(WakeTrigger.TIMER, cb, timeout_seconds=60)
        r2 = manager.register(WakeTrigger.EVENT, cb, event_key="test")
        r3 = manager.register(WakeTrigger.TIMER, cb, timeout_seconds=-1)

        # r3 已到期
        manager.check_due()
        pending = manager.pending()
        # r1 和 r2 仍 pending，r3 变为 DUE
        ids = [r.id for r in pending]
        assert r1.id in ids
        assert r2.id in ids
        assert r3.id in ids  # DUE 也算 pending

    @pytest.mark.asyncio
    async def test_pending_by_trigger(self, manager):
        """按触发模式过滤未触发记录"""
        async def cb():
            pass

        manager.register(WakeTrigger.TIMER, cb, timeout_seconds=60)
        manager.register(WakeTrigger.EVENT, cb, event_key="test")
        manager.register(WakeTrigger.COMPLETION, cb, job_id="job1")

        timers = manager.pending_by_trigger(WakeTrigger.TIMER)
        events = manager.pending_by_trigger(WakeTrigger.EVENT)
        completions = manager.pending_by_trigger(WakeTrigger.COMPLETION)

        assert len(timers) == 1
        assert len(events) == 1
        assert len(completions) == 1

    @pytest.mark.asyncio
    async def test_stats(self, manager):
        """统计信息"""
        async def cb():
            pass

        manager.register(WakeTrigger.TIMER, cb, timeout_seconds=60)
        manager.register(WakeTrigger.TIMER, cb, timeout_seconds=-1)

        manager.check_due()  # 一个变 DUE
        stats = manager.stats()
        assert stats["total"] == 2
        assert stats["pending"] == 1
        assert stats["due"] == 1
        assert stats["fired"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_fired(self, manager):
        """清理已触发记录"""
        async def cb():
            pass

        # 注册多个并全部 fire
        for _ in range(10):
            record = manager.register(WakeTrigger.TIMER, cb, timeout_seconds=-1)
            await manager.fire(record.id)

        # 清理到只保留 5 条
        cleaned = manager.cleanup_fired(max_records=5)
        assert cleaned > 0
        assert len(manager._records) <= 5

    @pytest.mark.asyncio
    async def test_callback_error_does_not_crash(self, manager):
        """回调抛异常不影响管理器"""
        async def bad_cb():
            raise RuntimeError("callback error")

        record = manager.register(
            trigger=WakeTrigger.TIMER,
            callback=bad_cb,
            timeout_seconds=-1,
        )
        manager.check_due()
        result = await manager.fire(record.id)
        assert result  # 仍然返回 True（fire 成功）
        assert record.state == WakeState.FIRED


class TestGetSelfWakeManager:
    """全局单例测试"""

    def test_singleton(self):
        """get_self_wake_manager 返回单例"""
        m1 = get_self_wake_manager()
        m2 = get_self_wake_manager()
        assert m1 is m2
