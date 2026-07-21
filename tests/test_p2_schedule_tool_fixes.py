"""P2-1 测试: schedule_tool.create_reminder 4 个问题修复。

Bug 列表:
1. docstring 过时: 说"user_id 隔离"，实际已改为全局可见
2. 空数组绕过 all() 检查: days=[] / channels=[] 应使用默认值
3. INSERT 后用 ORDER BY id DESC LIMIT 1 取新记录（竞态条件）
4. 死代码: uid = _resolve_user_id(user_id) 未使用
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_core():
    """构造 mock core 用于测试。"""
    core = MagicMock()
    core.db = MagicMock()
    core.db.execute = AsyncMock()
    # 默认 fetch_one 返回 None（测试 days/channels 默认值时不依赖 DB）
    core.db.fetch_one = AsyncMock(return_value=None)
    return core


@pytest.mark.asyncio
async def test_create_reminder_empty_days_uses_default():
    """days=[] 应使用默认值（1-7），而非通过 all() 检查。"""
    from tools import schedule_tool
    core = _make_core()
    with patch.object(schedule_tool, "_get_core", return_value=core):
        result = await schedule_tool.create_reminder(
            time="09:00", prompt_hint="测试", days=[], channels=["web"],
            user_id="test_user",
        )
    assert result.success, f"应成功创建: {result.error}"
    # 验证 INSERT 时 days_json 是 [1,2,3,4,5,6,7]
    call_args = core.db.execute.call_args
    args = call_args.args
    days_json = args[1][1]  # 第二个参数是 tuple
    assert json.loads(days_json) == [1, 2, 3, 4, 5, 6, 7], \
        f"days=[] 应使用默认 [1-7]，实际: {days_json}"


@pytest.mark.asyncio
async def test_create_reminder_empty_channels_uses_default():
    """channels=[] 应使用默认值 ['web']。"""
    from tools import schedule_tool
    core = _make_core()
    with patch.object(schedule_tool, "_get_core", return_value=core):
        result = await schedule_tool.create_reminder(
            time="09:00", prompt_hint="测试", days=[1, 2, 3], channels=[],
            user_id="test_user",
        )
    assert result.success, f"应成功创建: {result.error}"
    call_args = core.db.execute.call_args
    args = call_args.args
    channels_json = args[1][3]  # channels 是第 4 个参数
    assert json.loads(channels_json) == ["web"], \
        f"channels=[] 应使用默认 ['web']，实际: {channels_json}"


@pytest.mark.asyncio
async def test_create_reminder_uses_lastrowid_not_order_by_desc():
    """INSERT 后应用 lastrowid 精确查询，不用 ORDER BY id DESC（防竞态）。"""
    from tools import schedule_tool
    core = _make_core()
    # 模拟 execute 返回有 lastrowid 的 cursor
    mock_cursor = MagicMock()
    mock_cursor.lastrowid = 42
    core.db.execute = AsyncMock(return_value=mock_cursor)
    core.db.fetch_one = AsyncMock(return_value={"id": 42, "time": "09:00", "prompt_hint": "测试", "days": "[1]", "enabled": 1})

    with patch.object(schedule_tool, "_get_core", return_value=core):
        await schedule_tool.create_reminder(
            time="09:00", prompt_hint="测试", days=[1], channels=["web"],
            user_id="test_user",
        )

    # 验证 SELECT 用了 WHERE id=? 而非 ORDER BY id DESC
    select_call = core.db.fetch_one.call_args
    sql = select_call.args[0]
    assert "ORDER BY id DESC" not in sql, \
        f"应用 lastrowid 精确查询，不用 ORDER BY id DESC。SQL: {sql}"
    assert "WHERE id" in sql or "id = " in sql or "id=?" in sql.lower() or "id = ?" in sql, \
        f"应用 WHERE id = ? 精确查询。SQL: {sql}"


def test_create_reminder_docstring_updated():
    """docstring 不应再说 user_id 隔离（实际已全局可见）。"""
    from tools.schedule_tool import create_reminder
    doc = create_reminder.__doc__ or ""
    assert "user_id 隔离" not in doc, "docstring 不应再说 user_id 隔离"
    assert "隔离" not in doc or "不按用户隔离" in doc or "全局可见" in doc, \
        "docstring 应明确说明全局可见"


# Helper: patch
from unittest.mock import patch


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
