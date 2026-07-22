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
    """INSERT 后应用 lastrowid 精确查询，不用 ORDER BY id DESC（防竞态）。

    TRAE-code-review 修正：DatabaseManager.execute 返回 int（lastrowid 值），
    不是 cursor 对象。测试必须匹配真实返回类型，否则会假绿。
    CodeRabbit 二次扫描：移除 ORDER BY 兜底，falsy new_id 应返回错误而非取别人的记录。
    """
    from tools import schedule_tool
    core = _make_core()
    # 真实 DatabaseManager.execute 对 INSERT 返回 lastrowid (int)，不是 cursor
    core.db.execute = AsyncMock(return_value=42)  # lastrowid=42
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
    # 验证 fetch_one 第二参数是 lastrowid 值 (42) 作为 params 元组
    if select_call.args and len(select_call.args) > 1:
        params = select_call.args[1]
        assert params == (42,) or params == 42, \
            f"fetch_one 应传入 lastrowid=42 作为参数。实际: {params}"


@pytest.mark.asyncio
async def test_create_reminder_falsy_lastrowid_returns_error_not_fallback():
    """CodeRabbit F3: new_id 为 falsy 时应返回错误，而非走 ORDER BY id DESC 取别人的记录。

    场景：AUTOINCREMENT 表成功 INSERT 后 lastrowid 必 >0，但若异常返回 0/None，
    不应用 ORDER BY id DESC LIMIT 1（可能取到另一进程刚插入的记录），
    应直接返回失败让用户重试。
    """
    from tools import schedule_tool
    core = _make_core()
    # 模拟 execute 异常返回 0（falsy）
    core.db.execute = AsyncMock(return_value=0)
    core.db.fetch_one = AsyncMock()

    with patch.object(schedule_tool, "_get_core", return_value=core):
        result = await schedule_tool.create_reminder(
            time="09:00", prompt_hint="测试", days=[1], channels=["web"],
            user_id="test_user",
        )

    # 应返回失败，而非调用 fetch_one 走 ORDER BY 兜底
    assert not result.success, "falsy lastrowid 应返回失败，不应继续查询"
    assert "失败" in result.error or "错误" in result.error, \
        f"应返回错误信息。实际: {result.error}"
    # fetch_one 不应被调用（不应走 ORDER BY 兜底）
    core.db.fetch_one.assert_not_called(), \
        "falsy lastrowid 不应调用 fetch_one（消除 ORDER BY 竞态）"


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
