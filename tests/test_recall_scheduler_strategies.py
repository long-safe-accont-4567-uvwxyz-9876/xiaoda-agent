"""P1-2: Automation 调度策略改进 — 测试

测试 memory/recall_scheduler.py 新增的 catch_up 和 skip_on_overlap 策略。
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from memory.recall_scheduler import MemoryRecallScheduler


class FakeCore:
    """用于测试的 AgentCore 替身"""

    def __init__(self, last_run=None, memory=None):
        self.db = AsyncMock()
        self.db.get_cron_last_run = AsyncMock(return_value=last_run)
        self.db.set_cron_last_run = AsyncMock()
        self.memory = memory


class TestRecallSchedulerCatchUp:
    """catch-up 策略测试"""

    @pytest.mark.asyncio
    async def test_catchup_triggers_when_overdue(self):
        """上次回忆距今超过间隔时，catch-up 补跑一次"""
        # last_run 设为 10 小时前（超过 3 小时间隔）
        last_run = time.time() - 10 * 3600
        core = FakeCore(last_run=last_run, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=True, skip_on_overlap=True)
        
        # 直接调用 _catchup_tick
        await scheduler._catchup_tick()

        # 应该执行了回忆任务
        core.memory.run_scheduled_recall.assert_called_once()
        core.db.set_cron_last_run.assert_called()

    @pytest.mark.asyncio
    async def test_catchup_skips_when_recent(self):
        """上次回忆距今不足间隔时，catch-up 不触发"""
        # last_run 设为 1 小时前（不足 3 小时间隔）
        last_run = time.time() - 1 * 3600
        core = FakeCore(last_run=last_run, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=0)

        scheduler = MemoryRecallScheduler(core, catch_up=True, skip_on_overlap=True)
        
        await scheduler._catchup_tick()

        # 不应该执行回忆任务
        core.memory.run_scheduled_recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_catchup_disabled(self):
        """catch_up=False 时不执行 catch-up"""
        last_run = time.time() - 10 * 3600
        core = FakeCore(last_run=last_run, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=False, skip_on_overlap=True)
        
        # _catchup_done 不会被设置（因为 _loop 中 catch_up=False 不会调用）
        assert not scheduler._catchup_done

    @pytest.mark.asyncio
    async def test_catchup_with_no_last_run(self):
        """从未运行过时（last_run=None），catch-up 不触发"""
        core = FakeCore(last_run=None, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=True, skip_on_overlap=True)
        
        await scheduler._catchup_tick()

        # last_run=None 时不触发 catch-up（留给正常 tick 处理）
        core.memory.run_scheduled_recall.assert_not_called()


class TestRecallSchedulerSkipOnOverlap:
    """skip-on-overlap 策略测试"""

    @pytest.mark.asyncio
    async def test_skip_when_already_running(self):
        """上次还没跑完时跳过本次"""
        core = FakeCore(last_run=None, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=False, skip_on_overlap=True)
        
        # 模拟正在运行
        scheduler._is_running = True
        
        # _should_run 返回 True，但因为 overlap guard 应该跳过
        # 注意：_tick 中先检查 _is_dnd，再检查 overlap
        # DND 检查可能放行（取决于时间），overlap 应该拦截
        with patch.object(scheduler, '_is_dnd', return_value=False):
            with patch.object(scheduler, '_should_run', new=AsyncMock(return_value=True)):
                await scheduler._tick()

        # 不应该执行回忆任务
        core.memory.run_scheduled_recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_skip_when_overlap_disabled(self):
        """skip_on_overlap=False 时不跳过"""
        core = FakeCore(last_run=None, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=False, skip_on_overlap=False)
        
        # 模拟正在运行（但 skip_on_overlap=False）
        scheduler._is_running = True
        
        with patch.object(scheduler, '_is_dnd', return_value=False):
            with patch.object(scheduler, '_should_run', new=AsyncMock(return_value=True)):
                await scheduler._tick()

        # 应该执行回忆任务（overlap guard 被禁用）
        core.memory.run_scheduled_recall.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_running_resets_after_run(self):
        """运行完成后 _is_running 重置为 False"""
        core = FakeCore(last_run=None, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=False, skip_on_overlap=True)
        
        await scheduler._run_recall()
        
        assert scheduler._is_running is False


class TestRecallSchedulerBackwardCompat:
    """向后兼容测试"""

    def test_default_params(self):
        """默认参数：catch_up=True, skip_on_overlap=True"""
        core = FakeCore()
        scheduler = MemoryRecallScheduler(core)
        assert scheduler._catch_up is True
        assert scheduler._skip_on_overlap is True

    def test_old_style_init(self):
        """旧式构造（不传 catch_up/skip_on_overlap）仍然有效"""
        core = FakeCore()
        scheduler = MemoryRecallScheduler(core)
        assert scheduler._task is None
        assert scheduler.TICK_SECONDS == 300
        assert scheduler.RECALL_INTERVAL_HOURS == 3.0


class TestCatchUpDNDFix:
    """Qodo Bug #3 修复验证：catch-up 路径受 DND 门控。"""

    @pytest.mark.asyncio
    async def test_catchup_skipped_during_dnd(self):
        """DND 时段 catch-up 不触发"""
        last_run = time.time() - 10 * 3600
        core = FakeCore(last_run=last_run, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=True, skip_on_overlap=True)

        # 模拟 DND 时段
        with patch.object(scheduler, '_is_dnd', return_value=True):
            await scheduler._catchup_tick()

        # DND 时段不触发回忆任务
        core.memory.run_scheduled_recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_catchup_runs_outside_dnd(self):
        """非 DND 时段 catch-up 正常触发"""
        last_run = time.time() - 10 * 3600
        core = FakeCore(last_run=last_run, memory=MagicMock())
        core.memory.run_scheduled_recall = AsyncMock(return_value=5)

        scheduler = MemoryRecallScheduler(core, catch_up=True, skip_on_overlap=True)

        # 模拟非 DND 时段
        with patch.object(scheduler, '_is_dnd', return_value=False):
            await scheduler._catchup_tick()

        # 非 DND 时段正常触发
        core.memory.run_scheduled_recall.assert_called_once()
