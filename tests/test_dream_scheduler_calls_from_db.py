"""G7: dream scheduler 应调用 consolidate_from_db 而非 consolidate."""
from unittest.mock import AsyncMock, MagicMock, patch

from core.dream_consolidation import DreamConsolidator


def test_init_accepts_memory_db_param():
    """DreamConsolidator.__init__ 应接受 memory_db 参数."""
    mock_db = MagicMock()
    dc = DreamConsolidator(memory_db=mock_db)
    assert dc._memory_db is mock_db


async def test_scheduler_calls_consolidate_from_db():
    """scheduler 应优先调用 consolidate_from_db，不调 consolidate."""
    mock_db = MagicMock()
    dc = DreamConsolidator(memory_db=mock_db)

    # mock 两个方法，确认调用哪个
    dc.consolidate = AsyncMock(return_value={"archived": 0})
    dc.consolidate_from_db = AsyncMock(return_value={"archived": 0})

    # _run_scheduled_test 是一次性协程, 直接 await 即可.
    # 保留 time patch 以兼容未来可能的循环式 scheduler 测试.
    with patch("core.dream_consolidation.time") as mock_time:
        mock_time.localtime.return_value = type("TS", (), {
            "tm_year": 2026, "tm_mon": 7, "tm_mday": 20,
            "tm_hour": 3, "tm_min": 0, "tm_sec": 0,
            "tm_wday": 0, "tm_yday": 200, "tm_isdst": -1
        })()
        mock_time.mktime.return_value = 1000.0
        mock_time.time.return_value = 2000.0  # target < time，立即触发

        await dc._run_scheduled_test()

    # 验证调用 consolidate_from_db 而非 consolidate
    assert dc.consolidate_from_db.called
    assert not dc.consolidate.called


async def test_scheduler_fallback_to_consolidate_when_no_db():
    """无 memory_db 时降级调用 consolidate."""
    dc = DreamConsolidator(memory_db=None)
    dc.consolidate = AsyncMock(return_value={})
    dc.consolidate_from_db = AsyncMock(return_value={})

    with patch("core.dream_consolidation.time") as mock_time:
        mock_time.localtime.return_value = type("TS", (), {
            "tm_year": 2026, "tm_mon": 7, "tm_mday": 20,
            "tm_hour": 3, "tm_min": 0, "tm_sec": 0,
            "tm_wday": 0, "tm_yday": 200, "tm_isdst": -1
        })()
        mock_time.mktime.return_value = 1000.0
        mock_time.time.return_value = 2000.0

        await dc._run_scheduled_test()

    assert dc.consolidate.called
    assert not dc.consolidate_from_db.called
