"""提醒/日程工具 — 让 Agent 能查询、修改、删除 greeting_schedules 表中的 reminder 记录。

设计原则：
- 不预过滤数据，让 LLM 自己根据用户问题筛选（如"晚上"由 LLM 判断 time 字段）
- 复用 web/routers/schedule.py 的 HH:MM 校验正则，保证与 Web UI 一致
- 通过 bind(core) 注入 core 对象，与 memory_tool 模式相同
"""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

# 模块级 core 单例（由 core/bootstrap.py init 调用 bind 注入）
_core: Any = None

# HH:MM 校验正则，与 web/routers/schedule.py 保持一致
_HM_PATTERN = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def bind(core: Any) -> None:
    """由 core/bootstrap.py init 阶段调用，注入已初始化的 core 对象。"""
    global _core
    _core = core
    logger.info("schedule_tool.bound")


def _get_core() -> Any:
    """获取已注入的 core 实例，未注入则抛异常。"""
    if _core is None:
        raise RuntimeError("core 未初始化，请确认 core/bootstrap.py 已调用 schedule_tool.bind()")
    return _core


def _resolve_user_id(explicit: str = "") -> str:
    """解析当前请求的 user_id（已废弃，保留兼容）。

    修复 P2: reminder 不再按用户隔离，所有 reminder 全局可见。
    WebUI 和 agent 对话共享同一套提醒列表。
    """
    return "shared"  # 统一为 shared，不再隔离


def _validate_hm(value: str, field: str) -> None:
    """校验 HH:MM 格式，与 Web UI 校验逻辑一致。"""
    if not _HM_PATTERN.match(value or ""):
        raise ValueError(f"{field} 必须是 HH:MM 格式（如 19:30），当前值：{value!r}")


def _validate_days(days: list[int]) -> None:
    """校验 days 字段：必须是 1-7 的整数列表。"""
    if not isinstance(days, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
        raise ValueError(f"days 必须是 1-7 的整数数组（1=周一...7=周日），当前值：{days!r}")


def _format_row(row: dict) -> str:
    """格式化 reminder 行为可读字符串，供 LLM 理解。"""
    rid = row.get("id", "?")
    time_str = row.get("time", "")
    hint = row.get("prompt_hint", "")
    days_raw = row.get("days", "[1,2,3,4,5,6,7]")
    enabled = row.get("enabled", 1)
    try:
        days_list = json.loads(days_raw) if isinstance(days_raw, str) else days_raw
        days_str = ",".join(str(d) for d in days_list) if days_list else "无"
    except (ValueError, TypeError):
        days_str = str(days_raw)
    status = "启用" if enabled else "禁用"
    return f"[ID:{rid}] {time_str} | 周{days_str} | {status} | {hint}"


@register_tool(
    name="list_reminders",
    description=(
        "查询所有提醒（reminder 类型）。当用户问'晚上有什么任务'、'今天有什么提醒'、"
        "'我有几个提醒'、'提醒列表'等查询类问题时使用。"
        "返回的字段中 time 是 HH:MM 格式（如 19:30 表示晚上7点半），days 是星期几数组（1=周一...7=周日）。"
        "用户说的'晚上/早上/下午'等模糊时间词由你根据 time 字段判断，不要凭印象编造。"
    ),
    schema={
        "type": "object",
        "properties": {
            "include_disabled": {
                "type": "boolean",
                "description": "是否包含已禁用的提醒，默认 false（仅启用的）",
                "default": False,
            },
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="schedule",
    max_frequency=20,
)
async def list_reminders(include_disabled: bool = False,
                         user_id: str = "") -> ToolResult:
    """列出所有 reminder 提醒，按时间排序。

    修复 P2: 移除 user_id 隔离，所有 reminder 全局可见。
    """
    try:
        core = _get_core()
        if include_disabled:
            sql = ("SELECT id, time, prompt_hint, days, enabled, channels "
                   "FROM greeting_schedules WHERE type='reminder' "
                   "ORDER BY time")
            rows = await core.db.fetch_all(sql)
        else:
            sql = ("SELECT id, time, prompt_hint, days, enabled, channels "
                   "FROM greeting_schedules WHERE type='reminder' AND enabled=1 "
                   "ORDER BY time")
            rows = await core.db.fetch_all(sql)

        if not rows:
            return ToolResult.ok("当前没有任何提醒")

        formatted = [_format_row(r) for r in rows]
        total = len(formatted)
        header = f"共 {total} 条提醒（按时间排序）："
        return ToolResult.ok(header + "\n" + "\n".join(formatted))
    except Exception as e:
        logger.error("schedule_tool.list_failed", error=str(e))
        return ToolResult.fail(f"查询提醒失败：{e!s}")


@register_tool(
    name="create_reminder",
    description=(
        "创建新的提醒。当用户说'帮我设置一个提醒'、'提醒我晚上10点'、"
        "'每周三提醒我'等创建类指令时使用。"
        "创建后立即生效，返回新提醒的 ID 和详情。"
    ),
    schema={
        "type": "object",
        "properties": {
            "time": {
                "type": "string",
                "description": "提醒时间，HH:MM 格式（如 19:30、08:00）。必填。",
            },
            "prompt_hint": {
                "type": "string",
                "description": "提醒内容/标题（≤200字符）。必填。",
            },
            "days": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "星期列表（1=周一...7=周日，如 [1,3,5] 表示每周一三五）。默认每天 [1,2,3,4,5,6,7]。",
            },
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "推送渠道，默认 ['web']。可选 'web' 或 'qq'。",
            },
        },
        "required": ["time", "prompt_hint"],
    },
    permission=ToolPermission.READ_WRITE,
    category="schedule",
    max_frequency=10,
    requires_confirmation=True,
)
async def create_reminder(time: str, prompt_hint: str, days: list[int] | None = None,
                         channels: list[str] | None = None,
                         user_id: str = "") -> ToolResult:
    """创建新 reminder 记录。

    reminder 不按用户隔离，全局可见（与 list/update/delete 一致）。
    user_id 参数保留用于审计/日志，不影响数据归属。
    """
    import time as _time

    try:
        core = _get_core()

        # 1. 校验字段格式
        _validate_hm(time, "time")
        if not prompt_hint or not prompt_hint.strip():
            return ToolResult.fail("prompt_hint 不能为空")
        hint = prompt_hint[:200]

        # P2-1: 用 not days / not channels 而非 is None，处理空数组也走默认值
        # 避免 [] 通过 all() 检查导致 reminder 无活跃日/无渠道
        if not days:
            days_list = [1, 2, 3, 4, 5, 6, 7]
        else:
            _validate_days(days)
            days_list = sorted(set(days))

        if not channels:
            channels_list = ["web"]
        else:
            if not all(c in ("web", "qq") for c in channels):
                return ToolResult.fail("channels 仅支持 web/qq")
            channels_list = channels

        # 2. 插入数据库（不设置 user_id，全局可见）
        days_json = json.dumps(days_list)
        channels_json = json.dumps(channels_list)
        created_at = _time.time()

        # P2-1: DatabaseManager.execute 对 INSERT 返回 lastrowid (int)，直接用于精确查询
        # 避免 ORDER BY id DESC 的竞态条件（并发插入时可能取到别人的记录）
        new_id = await core.db.execute(
            "INSERT INTO greeting_schedules"
            "(type, time, days, prompt_hint, channels, enabled, next_fire_times, created_at) "
            "VALUES ('reminder', ?, ?, ?, ?, 1, '[]', ?)",
            (time, days_json, hint, channels_json, created_at))

        # new_id 是 lastrowid (int > 0 表示插入成功)
        if new_id:
            row = await core.db.fetch_one(
                "SELECT id, time, prompt_hint, days, enabled FROM greeting_schedules "
                "WHERE id = ?", (new_id,))
        else:
            # 极端兜底：execute 返回 0/None 时降级到 ORDER BY（保持原有兜底）
            row = await core.db.fetch_one(
                "SELECT id, time, prompt_hint, days, enabled FROM greeting_schedules "
                "WHERE type='reminder' ORDER BY id DESC LIMIT 1")

        formatted = _format_row(row) if row else f"已创建（时间：{time}）"
        return ToolResult.ok(f"已创建提醒：{formatted}")
    except ValueError as e:
        return ToolResult.fail(f"参数格式错误：{e!s}")
    except Exception as e:
        logger.error("schedule_tool.create_failed", error=str(e))
        return ToolResult.fail(f"创建提醒失败：{e!s}")


@register_tool(
    name="update_reminder",
    description=(
        "修改指定提醒的标题/时间/星期/启用状态。当用户说'帮我改一下'、'把晚上的提醒改成'、"
        "'关掉这个提醒'（禁用）、'改成每周三'等修改类指令时使用。"
        "只更新传入的字段，未传的字段保持不变。修改后立即生效。"
    ),
    schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "integer",
                "description": "要修改的提醒 ID（来自 list_reminders 返回的 ID）",
            },
            "time": {
                "type": "string",
                "description": "新的触发时间，HH:MM 格式（如 19:30、08:00）。不修改则不传。",
            },
            "prompt_hint": {
                "type": "string",
                "description": "新的提醒内容/标题（≤200字符）。不修改则不传。",
            },
            "days": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "新的星期列表（1=周一...7=周日，如 [1,3,5] 表示每周一三五）。不修改则不传。",
            },
            "enabled": {
                "type": "boolean",
                "description": "true=启用，false=禁用。不修改则不传。",
            },
        },
    "required": ["id"],
    },
    permission=ToolPermission.READ_WRITE,
    category="schedule",
    max_frequency=10,
    # 修复 P1：写操作加确认，防止 LLM 误改主人 reminder
    requires_confirmation=True,
)
async def update_reminder(id: int, time: str = "", prompt_hint: str = "",
                         days: list[int] | None = None,
                         enabled: bool | None = None,
                         user_id: str = "") -> ToolResult:
    """更新指定 reminder 的字段。仅更新传入的字段，未传字段保持不变。

    修复 P2: 移除 user_id 隔离，所有 reminder 全局可修改。
    """
    try:
        core = _get_core()

        # 1. 校验存在性
        existing = await core.db.fetch_one(
            "SELECT id, time, prompt_hint, days, enabled FROM greeting_schedules "
            "WHERE id=? AND type='reminder'",
            (id,))
        if not existing:
            return ToolResult.fail(f"提醒 ID {id} 不存在（或不是 reminder 类型）")

        # 2. 校验传入字段格式
        updates: dict[str, Any] = {}
        if time:
            _validate_hm(time, "time")
            updates["time"] = time
        if prompt_hint:
            updates["prompt_hint"] = prompt_hint[:200]
        if days is not None:
            _validate_days(days)
            updates["days"] = json.dumps(sorted(set(days)))
        if enabled is not None:
            updates["enabled"] = 1 if enabled else 0

        if not updates:
            return ToolResult.ok(f"未传入任何更新字段，提醒 ID {id} 保持不变")

        # 3. 拼接 UPDATE SQL（动态字段）
        set_clauses = []
        params: list[Any] = []
        for k, v in updates.items():
            set_clauses.append(f"{k}=?")
            params.append(v)
        # 触发 next_fire_times 重算（与 Web UI 一致）
        set_clauses.append("next_fire_times='[]'")
        params.append(id)

        sql = (f"UPDATE greeting_schedules SET {', '.join(set_clauses)} "
               f"WHERE id=? AND type='reminder'")
        n = await core.db.execute(sql, tuple(params))
        if not n:
            return ToolResult.fail(f"更新失败：提醒 ID {id} 不存在或已被删除")

        # 4. 查询返回更新后的记录
        row = await core.db.fetch_one(
            "SELECT id, time, prompt_hint, days, enabled FROM greeting_schedules "
            "WHERE id=?",
            (id,))
        formatted = _format_row(row) if row else f"ID:{id} (已更新)"
        return ToolResult.ok(f"已更新：{formatted}")
    except ValueError as e:
        # 输入格式错误，不写日志
        return ToolResult.fail(f"参数格式错误：{e!s}")
    except Exception as e:
        logger.error("schedule_tool.update_failed", id=id, error=str(e))
        return ToolResult.fail(f"修改提醒失败：{e!s}")


@register_tool(
    name="delete_reminder",
    description=(
        "删除指定提醒。当用户说'删掉这个提醒'、'取消晚上的提醒'、'不要这个了'等删除类指令时使用。"
        "删除后无法恢复，请先调用 list_reminders 确认 ID 后再调用本工具。"
    ),
    schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "integer",
                "description": "要删除的提醒 ID（来自 list_reminders 返回的 ID）",
            },
        },
        "required": ["id"],
    },
    permission=ToolPermission.READ_WRITE,
    category="schedule",
    max_frequency=5,
)
async def delete_reminder(id: int, user_id: str = "") -> ToolResult:
    """删除指定 reminder 记录。

    修复 P2: 移除 user_id 隔离，所有 reminder 全局可删除。
    """
    try:
        core = _get_core()

        # 1. 校验存在性 + 类型
        existing = await core.db.fetch_one(
            "SELECT id, time, prompt_hint FROM greeting_schedules "
            "WHERE id=? AND type='reminder'",
            (id,))
        if not existing:
            return ToolResult.fail(f"提醒 ID {id} 不存在（或不是 reminder 类型）")

        # 2. 删除
        n = await core.db.execute(
            "DELETE FROM greeting_schedules WHERE id=? AND type='reminder'",
            (id,))
        if not n:
            return ToolResult.fail(f"删除失败：提醒 ID {id} 不存在或已被删除")

        hint = existing.get("prompt_hint", "")
        time_str = existing.get("time", "")
        return ToolResult.ok(f"已删除提醒 ID {id}（{time_str} - {hint}）")
    except Exception as e:
        logger.error("schedule_tool.delete_failed", id=id, error=str(e))
        return ToolResult.fail(f"删除提醒失败：{e!s}")
