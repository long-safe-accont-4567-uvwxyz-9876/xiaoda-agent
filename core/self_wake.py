"""Self-Wake 挂起/恢复机制 — 借鉴 OpenWorker coworker/selfwake.py 设计。

将 always-on 的后台循环改为事件驱动，减少无谓的 LLM 调用和 CPU 占用。
支持三种唤醒触发模式：TIMER / COMPLETION / EVENT。

设计要点：
- ``WakeTrigger`` 枚举定义三种触发模式
- ``SelfWakeManager`` 管理唤醒记录的注册、检查、触发
- ``WakeRecord`` 记录每次唤醒的元数据
- 在 ``core/background_tasks.py`` 中集成，将部分常驻循环改为事件驱动
- 保持向后兼容：不使用 SelfWake 的模块继续走原有逻辑
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from loguru import logger


class WakeTrigger(str, Enum):
    """唤醒触发模式枚举 — 借鉴 OpenWorker selfwake.py 的三种触发。

    - TIMER: 定时唤醒（替代常驻循环中的 sleep+poll）
    - COMPLETION: 某个异步任务完成后唤醒
    - EVENT: 外部事件触发（如用户消息到达）
    """
    TIMER = "timer"
    COMPLETION = "completion"
    EVENT = "event"


class WakeState(str, Enum):
    """唤醒记录状态。"""
    PENDING = "pending"    # 等待触发
    DUE = "due"            # 已到触发时间/条件满足，待处理
    FIRED = "fired"        # 已触发回调


@dataclass
class WakeRecord:
    """唤醒记录 — 注册一次唤醒的完整上下文。"""
    id: str
    trigger: WakeTrigger
    callback: Callable[[], Coroutine[Any, Any, None]]
    # TIMER: 触发时间戳；COMPLETION/EVENT 不使用
    fire_at: float = 0.0
    # COMPLETION: 关联的 job_id；TIMER/EVENT 不使用
    job_id: str = ""
    # EVENT: 关联的 event_key；TIMER/COMPLETION 不使用
    event_key: str = ""
    # 超时时间戳（0 表示无超时）
    timeout_at: float = 0.0
    # 备注
    note: str = ""
    # 状态
    state: WakeState = WakeState.PENDING
    # 创建时间
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        """是否已超时（仅对设了 timeout_at 的记录有效）。"""
        if self.timeout_at == 0:
            return False
        return time.time() > self.timeout_at

    @property
    def is_due(self) -> bool:
        """是否已到触发条件。"""
        if self.state == WakeState.DUE:
            return True
        if self.state != WakeState.PENDING:
            return False
        if self.trigger == WakeTrigger.TIMER:
            return time.time() >= self.fire_at
        # COMPLETION 和 EVENT 通过 complete_job/fire_event 显式标记为 DUE
        return False


class SelfWakeManager:
    """Self-Wake 管理器 — 注册、检查、触发唤醒回调。

    用法：
        manager = SelfWakeManager()

        # 注册定时唤醒
        manager.register(
            trigger=WakeTrigger.TIMER,
            callback=my_async_callback,
            timeout_seconds=3600,  # 1小时后唤醒
        )

        # 注册任务完成唤醒
        manager.register(
            trigger=WakeTrigger.COMPLETION,
            callback=my_callback,
            job_id="task_123",
        )

        # 注册事件唤醒
        manager.register(
            trigger=WakeTrigger.EVENT,
            callback=my_callback,
            event_key="user_message",
        )

        # 在主循环中检查到期唤醒
        due_records = manager.check_due()
        for record in due_records:
            await manager.fire(record.id)

        # 外部事件触发
        manager.fire_event("user_message")
        manager.complete_job("task_123")
    """

    def __init__(self) -> None:
        self._records: dict[str, WakeRecord] = {}

    def register(
        self,
        trigger: WakeTrigger,
        callback: Callable[[], Coroutine[Any, Any, None]],
        *,
        timeout_seconds: float | None = None,
        job_id: str = "",
        event_key: str = "",
        note: str = "",
    ) -> WakeRecord:
        """注册一个唤醒记录。

        Args:
            trigger: 触发模式
            callback: 唤醒时调用的异步回调
            timeout_seconds: TIMER 模式下多少秒后唤醒；其他模式忽略
            job_id: COMPLETION 模式下关联的任务 ID
            event_key: EVENT 模式下关联的事件键
            note: 备注

        Returns:
            WakeRecord — 注册的唤醒记录
        """
        record_id = uuid.uuid4().hex[:16]
        fire_at = 0.0
        timeout_at = 0.0
        if trigger == WakeTrigger.TIMER:
            # TIMER 模式必须指定 timeout_seconds，否则 fire_at=0 会立即触发
            if timeout_seconds is None:
                raise ValueError("timeout_seconds is required for TIMER trigger")
            fire_at = time.time() + timeout_seconds
            # TIMER 记录在触发时间后 5 分钟未执行则过期清理，避免堆积
            timeout_at = fire_at + 300.0

        record = WakeRecord(
            id=record_id,
            trigger=trigger,
            callback=callback,
            fire_at=fire_at,
            timeout_at=timeout_at,
            job_id=job_id,
            event_key=event_key,
            note=note,
        )
        self._records[record_id] = record
        logger.debug("selfwake.registered",
                      id=record_id, trigger=trigger.value,
                      fire_at=fire_at if fire_at else None,
                      job_id=job_id or None,
                      event_key=event_key or None)
        return record

    def check_due(self) -> list[WakeRecord]:
        """检查所有到期的唤醒记录（TIMER/COMPLETION/EVENT 模式）。

        Returns:
            到期的 WakeRecord 列表（不含已 fired 的）。
        """
        now = time.time()
        due = []
        for record in self._records.values():
            if record.state == WakeState.FIRED:
                continue
            # 已被 complete_job/fire_event 标记为 DUE 的记录优先处理
            # （即使已过期也先触发，避免回调被静默跳过）
            if record.state == WakeState.DUE:
                due.append(record)
                continue
            if record.is_expired:
                record.state = WakeState.FIRED
                logger.debug("selfwake.expired_skipped",
                              id=record.id, trigger=record.trigger.value)
                continue
            # TIMER 模式：检查是否到时
            if (record.state == WakeState.PENDING
                    and record.trigger == WakeTrigger.TIMER
                    and now >= record.fire_at):
                record.state = WakeState.DUE
                due.append(record)
        return due

    async def fire(self, record_id: str) -> bool:
        """触发一个唤醒记录的回调。

        Args:
            record_id: 唤醒记录 ID

        Returns:
            是否成功触发（已 fired 或不存在则返回 False）
        """
        record = self._records.get(record_id)
        if record is None or record.state == WakeState.FIRED:
            return False

        record.state = WakeState.FIRED
        try:
            await record.callback()
        except Exception as e:
            logger.warning("selfwake.callback_failed",
                           id=record_id, error=str(e))
        return True

    def complete_job(self, job_id: str) -> list[WakeRecord]:
        """标记某个任务完成，触发关联的 COMPLETION 唤醒。

        Args:
            job_id: 完成的任务 ID

        Returns:
            被标记为 DUE 的唤醒记录列表。
        """
        fired = []
        for record in self._records.values():
            if (record.state == WakeState.PENDING
                    and record.trigger == WakeTrigger.COMPLETION
                    and record.job_id == job_id):
                record.state = WakeState.DUE
                fired.append(record)
        if fired:
            logger.debug("selfwake.job_completed",
                          job_id=job_id, woke=len(fired))
        return fired

    def fire_event(self, event_key: str) -> list[WakeRecord]:
        """触发某个事件，触发关联的 EVENT 唤醒。

        Args:
            event_key: 事件键

        Returns:
            被标记为 DUE 的唤醒记录列表。
        """
        fired = []
        for record in self._records.values():
            if (record.state == WakeState.PENDING
                    and record.trigger == WakeTrigger.EVENT
                    and record.event_key == event_key):
                record.state = WakeState.DUE
                fired.append(record)
        if fired:
            logger.debug("selfwake.event_fired",
                          event_key=event_key, woke=len(fired))
        return fired

    def cancel(self, record_id: str) -> bool:
        """取消一个唤醒记录。

        Args:
            record_id: 唤醒记录 ID

        Returns:
            是否成功取消
        """
        record = self._records.get(record_id)
        if record is None or record.state == WakeState.FIRED:
            return False
        record.state = WakeState.FIRED
        return True

    def pending(self) -> list[WakeRecord]:
        """返回所有未触发的唤醒记录。"""
        return [r for r in self._records.values() if r.state != WakeState.FIRED]

    def pending_by_trigger(self, trigger: WakeTrigger) -> list[WakeRecord]:
        """返回指定触发模式的未触发唤醒记录。"""
        return [r for r in self.pending() if r.trigger == trigger]

    def cleanup_fired(self, max_records: int = 200) -> int:
        """清理已触发的记录，防止内存泄漏。

        Args:
            max_records: 保留的最大记录数（含已 fired）

        Returns:
            清理的记录数
        """
        if len(self._records) <= max_records:
            return 0
        fired_ids = [rid for rid, r in self._records.items()
                     if r.state == WakeState.FIRED]
        # 保留最新的记录，删除最旧的 fired 记录
        remove_count = len(self._records) - max_records
        for rid in fired_ids[:remove_count]:
            del self._records[rid]
        return min(remove_count, len(fired_ids))

    def stats(self) -> dict[str, int]:
        """返回统计信息。"""
        return {
            "total": len(self._records),
            "pending": sum(1 for r in self._records.values()
                           if r.state == WakeState.PENDING),
            "due": sum(1 for r in self._records.values()
                       if r.state == WakeState.DUE),
            "fired": sum(1 for r in self._records.values()
                         if r.state == WakeState.FIRED),
        }


# ── 全局单例 ──────────────────────────────────────────────

_default_manager: SelfWakeManager | None = None


def get_self_wake_manager() -> SelfWakeManager:
    """获取全局 SelfWakeManager 单例。"""
    global _default_manager
    if _default_manager is None:
        _default_manager = SelfWakeManager()
    return _default_manager
