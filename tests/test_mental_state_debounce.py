"""G3: mental_state debounce 测试 - 多次更新合并为 1 次写盘."""
import threading
import time
from unittest.mock import patch

from core.mental_state import MentalStateManager


def test_save_debounces_multiple_calls(tmp_path):
    """300ms 内连续 10 次 _save 只触发 1 次磁盘写入."""
    mgr = MentalStateManager(data_dir=tmp_path)
    call_count = 0
    original_save = mgr._state.save  # 绑定方法

    def counting_save(path):
        nonlocal call_count
        call_count += 1
        original_save(path)

    try:
        # patch 实例属性（非类属性），避免被其他测试的 pending Timer 污染
        with patch.object(mgr._state, 'save', counting_save):
            for _ in range(10):
                mgr._save()
            # debounce 窗口内尚未写盘
            assert call_count == 0, "debounce 窗口内不应立即写盘"
            # 等待 debounce 窗口
            time.sleep(0.5)
            assert call_count == 1, f"应只写盘 1 次，实际 {call_count}"
    finally:
        mgr.flush()


def test_flush_writes_immediately(tmp_path):
    """flush() 立即触发写盘，不等 debounce."""
    mgr = MentalStateManager(data_dir=tmp_path)
    call_count = 0

    def counting_save(path):
        nonlocal call_count
        call_count += 1

    try:
        with patch.object(mgr._state, 'save', counting_save):
            mgr._save()
            assert call_count == 0, "debounce 窗口内不应写盘"
            mgr.flush()
            assert call_count == 1, "flush 后应立即写盘"
    finally:
        mgr.flush()


def test_no_deadlock_on_concurrent_saves(tmp_path):
    """并发 _save 不死锁."""
    mgr = MentalStateManager(data_dir=tmp_path)

    def worker():
        for _ in range(100):
            mgr._save()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    mgr.flush()
    # 不抛异常即通过
