# EventBus 架构优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于 Coze Agent 架构优势，以最小改动实现 EventBus 事件总线、流式工具透明化、CancelToken 子代理中止、BeliefRouter 自适应路由修复、SharedBlackboard SQLite 背板 5 项优化。

**Architecture:** EventBus 定向投递给 User（非订阅/广播），User 按渠道类型决定投递方式（CLI 打印/Web ws 推送/QQ 仅开始通知）。现有 `status_callback` 保留用于 `stream_text` 流式文本，工具/子代理生命周期事件迁移到 EventBus。BeliefRouter 修复 `decide()` → `select_agent()` bug 并增加反馈回路。SharedBlackboard 增加 SQLite 背板支持跨进程。

**Tech Stack:** Python 3.11+, asyncio, ContextVar, dataclasses, sqlite3, loguru, pytest

## Global Constraints

- QQ Bot 消息频率封顶 5 条/轮，子代理事件仅开始时通知，不额外消耗消息条数
- EventBus 是全局单例，使用 ContextVar 绑定当前 session 的 User
- 不改变现有 dispatcher 调用逻辑，只在上下层加 emit
- 保留现有 `status_callback` 用于 `stream_text`，不破坏流式输出
- BeliefRouter 反馈判定：非空回复 = 成功，异常或空回复 = 失败
- CancelToken 支持超时自动取消 + 主 Agent 主动取消
- SharedBlackboard SQLite 背板复用现有 DB 路径，WAL 模式
- 所有时间相关函数使用 `ZoneInfo("Asia/Shanghai")`
- 子代理名替换：'纳西妲' → '小妲' 在所有输出路径生效

---

## File Structure

### 新增文件

| 文件 | 职责 |
|------|------|
| `core/event_bus.py` | AgentEventBus 单例 + AgentEvent 数据类 + AgentEventType 枚举 |
| `agent_core/user_base.py` | UserBase ABC + AGENT_DISPLAY 映射 + STATUS_ICON 映射 |
| `agent_core/user_cli.py` | CLIUser — 每个事件实时 rich print |
| `agent_core/user_web.py` | WebUser — 每个事件 WebSocket send_json 推送 |
| `agent_core/user_qq.py` | QQUser — 仅 SUB_STARTED 通知，其余静默 |
| `core/cancel_token.py` | CancelToken — 协程取消令牌 + 超时控制 |
| `agent_core/shared_blackboard_db.py` | SharedBlackboardDB — SQLite 背板黑板实现 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `agent_core/sub_agent_manager.py` | 3 处 dispatch 加 emit SUB_* 事件 + CancelToken 集成 |
| `tool_engine/tool_call_handler.py` | `_notify_tool_status` 改为 emit TOOL_* 到 EventBus |
| `core/router_engine.py` | L201: `decide()` → `select_agent()` bug 修复 |
| `agent_core/shared_blackboard.py` | 增加 SQLite 背板选项 |
| `qq_bot_adapter.py` | session 开始时 bind QQUser |
| `web/ws_hub.py` | session 开始时 bind WebUser |
| `cli.py`（或主入口） | session 开始时 bind CLIUser |

---

## Task 1: 创建 EventBus 核心 — `core/event_bus.py`

**Files:**
- Create: `core/event_bus.py`
- Test: `tests/test_event_bus.py`

**Interfaces:**
- Produces: `event_bus` (全局单例), `AgentEvent`, `AgentEventType`, `gen_task_id()`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_bus.py
"""EventBus 核心测试 — 定向投递给 User，非订阅/广播。"""
import pytest
from core.event_bus import event_bus, AgentEvent, AgentEventType, gen_task_id


class FakeUser:
    """测试用 FakeUser，记录收到的所有事件。"""
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def deliver(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_emit_without_user_silently_ignores():
    """没有绑定 User 时，emit 静默忽略，不抛异常。"""
    event_bus.unbind_user()  # 确保无绑定
    await event_bus.emit(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="test_123",
    ))
    # 无异常即通过


@pytest.mark.asyncio
async def test_emit_delivers_to_bound_user():
    """emit 后事件投递给绑定的 User。"""
    user = FakeUser()
    event_bus.bind_user(user)
    try:
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent="xiaolang",
            task_id="test_456",
            data={"display_name": "小狼"},
        ))
        assert len(user.events) == 1
        assert user.events[0].type == AgentEventType.SUB_STARTED
        assert user.events[0].agent == "xiaolang"
        assert user.events[0].data["display_name"] == "小狼"
    finally:
        event_bus.unbind_user()


@pytest.mark.asyncio
async def test_unbind_stops_delivery():
    """unbind_user 后不再投递。"""
    user = FakeUser()
    event_bus.bind_user(user)
    event_bus.unbind_user()
    await event_bus.emit(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="test_789",
    ))
    assert len(user.events) == 0


@pytest.mark.asyncio
async def test_emit_user_deliver_error_does_not_raise():
    """User.deliver() 异常不中断调用方。"""
    class BrokenUser:
        async def deliver(self, event):
            raise RuntimeError("broken")

    event_bus.bind_user(BrokenUser())
    try:
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent="xiaoke",
            task_id="test_000",
        ))
        # 无异常即通过
    finally:
        event_bus.unbind_user()


def test_gen_task_id_format():
    """gen_task_id 返回 {agent}_{8hex} 格式。"""
    task_id = gen_task_id("xiaolang")
    assert task_id.startswith("xiaolang_")
    assert len(task_id) == len("xiaolang_") + 8


def test_event_type_values():
    """AgentEventType 枚举值正确。"""
    assert AgentEventType.SUB_STARTED == "sub_started"
    assert AgentEventType.SUB_COMPLETED == "sub_completed"
    assert AgentEventType.SUB_FAILED == "sub_failed"
    assert AgentEventType.SUB_CANCELLED == "sub_cancelled"
    assert AgentEventType.TOOL_STARTED == "tool_started"
    assert AgentEventType.TOOL_COMPLETED == "tool_completed"
    assert AgentEventType.TOOL_FAILED == "tool_failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_event_bus.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.event_bus'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/event_bus.py
"""AgentEventBus — 子代理生命周期事件总线。
解耦主会话与子代理调度，事件定向投递给当前 User：

设计原则：
- EventBus 不绑定传输层，不搞订阅/广播
- emit 时找到当前 session 的 User，调用 user.deliver(event)
- User 按自身渠道类型决定投递方式（CLI直接打印/Web ws推送/QQ仅开始通知）
- 事件类型严格定义，不传任意 dict
- 本地部署单用户项目，永远只有一个消费者：当前 User
"""
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.user_base import UserBase


class AgentEventType(str, Enum):
    """子代理事件类型枚举。"""
    SUB_STARTED = "sub_started"
    SUB_PROGRESS = "sub_progress"
    SUB_COMPLETED = "sub_completed"
    SUB_FAILED = "sub_failed"
    SUB_CANCELLED = "sub_cancelled"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"


@dataclass
class AgentEvent:
    """子代理事件数据类。

    Attributes:
        type: 事件类型
        agent: 目标子代理名（xiaoli/xiaolang 等）
        task_id: 任务唯一标识
        data: 事件附加数据（tool_name/result_preview 等）
        timestamp: 事件时间戳
    """
    type: AgentEventType
    agent: str
    task_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# 当前 session 绑定的 User（ContextVar 实现协程安全）
_current_user: ContextVar["UserBase | None"] = ContextVar("event_bus_user", default=None)


class AgentEventBus:
    """子代理事件总线 — 定向投递，不是广播。

    使用方式：
        # 全局单例
        bus = AgentEventBus()

        # session 开始时绑定 User
        bus.bind_user(user)

        # 发射事件 → 自动投递给绑定的 User
        await bus.emit(AgentEvent(type=AgentEventType.SUB_STARTED, ...))

        # session 结束时解绑
        bus.unbind_user()
    """

    def bind_user(self, user: "UserBase") -> None:
        """绑定当前 session 的 User。"""
        _current_user.set(user)

    def unbind_user(self) -> None:
        """解绑 User（session 结束时调用）。"""
        _current_user.set(None)

    @property
    def bound_user(self) -> "UserBase | None":
        """当前绑定的 User。"""
        return _current_user.get()

    async def emit(self, event: AgentEvent) -> None:
        """发射事件，投递给当前绑定的 User。

        如果没有绑定 User（比如初始化阶段），静默忽略。
        User.deliver() 异常不中断调用方。
        """
        user = _current_user.get()
        if user is None:
            return
        try:
            await user.deliver(event)
        except Exception as e:
            logger.debug("event_bus.deliver_error type={} error={}",
                         event.type, str(e)[:100])


def gen_task_id(agent: str, input_hint: str = "") -> str:
    """生成任务唯一标识。"""
    return f"{agent}_{uuid.uuid4().hex[:8]}"


# ── 全局单例 ──────────────────────────────────────────────────
event_bus = AgentEventBus()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_event_bus.py -v --no-header --timeout=10`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add core/event_bus.py tests/test_event_bus.py && git commit -m "feat: add AgentEventBus with ContextVar-based User binding"
```

---

## Task 2: 创建 UserBase 基类 — `agent_core/user_base.py`

**Files:**
- Create: `agent_core/user_base.py`
- Test: `tests/test_user_base.py`

**Interfaces:**
- Consumes: `AgentEvent` from Task 1
- Produces: `UserBase` (ABC with `deliver()`), `AGENT_DISPLAY`, `STATUS_ICON`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_base.py
"""UserBase 基类测试。"""
import pytest
from agent_core.user_base import UserBase, AGENT_DISPLAY, STATUS_ICON
from core.event_bus import AgentEvent, AgentEventType


def test_agent_display_contains_all_agents():
    """AGENT_DISPLAY 包含所有子代理的显示名。"""
    assert AGENT_DISPLAY["xiaoli"] == "小莉"
    assert AGENT_DISPLAY["xiaolang"] == "小狼"
    assert AGENT_DISPLAY["xiaolian"] == "小涟"
    assert AGENT_DISPLAY["xiaoke"] == "小可"
    assert AGENT_DISPLAY["xiaoda"] == "小妲"


def test_status_icon_contains_all_event_types():
    """STATUS_ICON 包含所有事件类型的图标。"""
    assert STATUS_ICON["sub_started"] == "🔄"
    assert STATUS_ICON["sub_completed"] == "✅"
    assert STATUS_ICON["sub_failed"] == "❌"
    assert STATUS_ICON["sub_cancelled"] == "🚫"
    assert STATUS_ICON["tool_started"] == "🔧"
    assert STATUS_ICON["tool_completed"] == "✓"
    assert STATUS_ICON["tool_failed"] == "✗"


def test_userbase_is_abstract():
    """UserBase 是抽象类，不能直接实例化。"""
    with pytest.raises(TypeError):
        UserBase()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_base.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.user_base'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/user_base.py
"""User 基类 — EventBus 事件投递的统一入口。
各渠道 User 继承此基类，实现 deliver(event) 决定如何投递事件：
- CLIUser：每个事件直接 rich print（无消息条数限制）
- WebUser：每个事件 ws.send_json 推送（无消息条数限制）
- QQUser：仅 SUB_STARTED 通知，其余静默（节省5条限制）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_bus import AgentEvent


class UserBase(ABC):
    """User 基类 — 所有渠道 User 的父类。"""

    @abstractmethod
    async def deliver(self, event: "AgentEvent") -> None:
        """投递事件 — 由 EventBus.emit() 调用。

        各渠道 User 按自身特性实现：
        - CLIUser：实时打印
        - WebUser：WebSocket 推送
        - QQUser：仅开始时通知
        """
        ...


# 子代理显示名映射（所有渠道共用）
AGENT_DISPLAY: dict[str, str] = {
    "xiaoli": "小莉",
    "xiaolang": "小狼",
    "xiaolian": "小涟",
    "xiaoke": "小可",
    "xiaoda": "小妲",
}

# 紧凑图标映射（QQ 聚合模式用）
STATUS_ICON: dict[str, str] = {
    "sub_started": "🔄",
    "sub_completed": "✅",
    "sub_failed": "❌",
    "sub_cancelled": "🚫",
    "tool_started": "🔧",
    "tool_completed": "✓",
    "tool_failed": "✗",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_base.py -v --no-header --timeout=10`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/user_base.py tests/test_user_base.py && git commit -m "feat: add UserBase ABC with AGENT_DISPLAY and STATUS_ICON mappings"
```

---

## Task 3: 创建 CLIUser — `agent_core/user_cli.py`

**Files:**
- Create: `agent_core/user_cli.py`
- Test: `tests/test_user_cli.py`

**Interfaces:**
- Consumes: `UserBase` from Task 2, `AgentEvent` from Task 1
- Produces: `CLIUser`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_cli.py
"""CLIUser 测试 — 每个事件实时打印。"""
import pytest
from agent_core.user_cli import CLIUser
from core.event_bus import AgentEvent, AgentEventType


@pytest.mark.asyncio
async def test_cli_user_sub_started_prints(capsys):
    """SUB_STARTED 事件打印 🔄 {display}正在思考..."""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="t1",
        data={"display_name": "小狼"},
    ))
    captured = capsys.readouterr()
    assert "🔄" in captured.out
    assert "小狼" in captured.out
    assert "思考" in captured.out


@pytest.mark.asyncio
async def test_cli_user_sub_completed_prints(capsys):
    """SUB_COMPLETED 事件打印 ✅ {display}回复完成"""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="t2",
    ))
    captured = capsys.readouterr()
    assert "✅" in captured.out
    assert "小莉" in captured.out
    assert "完成" in captured.out


@pytest.mark.asyncio
async def test_cli_user_tool_started_prints(capsys):
    """TOOL_STARTED 事件打印 🔧 正在调用{tool}..."""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_STARTED,
        agent="xiaoke",
        task_id="t3",
        data={"tool_name": "web_search"},
    ))
    captured = capsys.readouterr()
    assert "🔧" in captured.out
    assert "web_search" in captured.out


@pytest.mark.asyncio
async def test_cli_user_uses_agent_display_fallback(capsys):
    """没有 display_name 时使用 AGENT_DISPLAY 映射。"""
    user = CLIUser()
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaoda",
        task_id="t4",
    ))
    captured = capsys.readouterr()
    assert "小妲" in captured.out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_cli.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.user_cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/user_cli.py
"""CLI User — 每个事件实时打印，无消息条数限制。"""
from __future__ import annotations

from core.event_bus import AgentEvent, AgentEventType
from agent_core.user_base import UserBase, AGENT_DISPLAY


class CLIUser(UserBase):
    """CLI 端：每个事件直接打印。"""

    async def deliver(self, event: AgentEvent) -> None:
        display = event.data.get("display_name") or AGENT_DISPLAY.get(event.agent, event.agent)

        if event.type == AgentEventType.SUB_STARTED:
            print(f"  🔄 {display}正在思考...")
        elif event.type == AgentEventType.SUB_COMPLETED:
            print(f"  ✅ {display}回复完成")
        elif event.type == AgentEventType.SUB_FAILED:
            print(f"  ❌ {display}遇到了问题")
        elif event.type == AgentEventType.SUB_CANCELLED:
            print(f"  🚫 {display}被取消了")
        elif event.type == AgentEventType.TOOL_STARTED:
            tool = event.data.get("tool_name", "")
            print(f"  🔧 正在调用{tool}...")
        elif event.type == AgentEventType.TOOL_COMPLETED:
            tool = event.data.get("tool_name", "")
            print(f"  ✓ {tool}完成")
        elif event.type == AgentEventType.TOOL_FAILED:
            tool = event.data.get("tool_name", "")
            print(f"  ✗ {tool}失败")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_cli.py -v --no-header --timeout=10`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/user_cli.py tests/test_user_cli.py && git commit -m "feat: add CLIUser with full event printing"
```

---

## Task 4: 创建 WebUser — `agent_core/user_web.py`

**Files:**
- Create: `agent_core/user_web.py`
- Test: `tests/test_user_web.py`

**Interfaces:**
- Consumes: `UserBase` from Task 2, `AgentEvent` from Task 1
- Produces: `WebUser`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_web.py
"""WebUser 测试 — 每个事件通过 WebSocket 推送。"""
import pytest
from agent_core.user_web import WebUser
from core.event_bus import AgentEvent, AgentEventType


@pytest.mark.asyncio
async def test_web_user_sub_started_sends_json():
    """SUB_STARTED 事件通过 send_fn 推送 dict。"""
    sent: list[dict] = []

    async def fake_send(event: dict) -> None:
        sent.append(event)

    user = WebUser(send_fn=fake_send)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="t1",
        data={"display_name": "小狼", "input_preview": "写代码"},
    ))
    assert len(sent) == 1
    assert sent[0]["type"] == "sub_started"
    assert sent[0]["agent"] == "xiaolang"
    assert sent[0]["display_name"] == "小狼"


@pytest.mark.asyncio
async def test_web_user_tool_started_sends_json():
    """TOOL_STARTED 事件通过 send_fn 推送 dict。"""
    sent: list[dict] = []

    async def fake_send(event: dict) -> None:
        sent.append(event)

    user = WebUser(send_fn=fake_send)
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_STARTED,
        agent="xiaoke",
        task_id="t2",
        data={"tool_name": "web_search"},
    ))
    assert len(sent) == 1
    assert sent[0]["type"] == "tool_started"
    assert sent[0]["tool_name"] == "web_search"


@pytest.mark.asyncio
async def test_web_user_send_error_does_not_raise():
    """send_fn 异常不中断调用方。"""
    async def broken_send(event: dict) -> None:
        raise RuntimeError("ws closed")

    user = WebUser(send_fn=broken_send)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="t3",
    ))
    # 无异常即通过
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_web.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.user_web'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/user_web.py
"""Web User — 每个事件通过 WebSocket 推送，无消息条数限制。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from core.event_bus import AgentEvent
from agent_core.user_base import UserBase, AGENT_DISPLAY


class WebUser(UserBase):
    """Web 端：每个事件通过 ws.send_json 推送。

    Args:
        send_fn: WebSocket 发送函数，签名为 async (event: dict) -> None
    """

    def __init__(self, send_fn: Callable[[dict], Awaitable[None]]) -> None:
        self._send_fn = send_fn

    async def deliver(self, event: AgentEvent) -> None:
        display = event.data.get("display_name") or AGENT_DISPLAY.get(event.agent, event.agent)
        payload: dict[str, Any] = {
            "type": event.type.value,
            "agent": event.agent,
            "task_id": event.task_id,
            "display_name": display,
            "timestamp": event.timestamp,
        }
        # 合并 event.data 中的字段（tool_name, input_preview 等）
        for k, v in event.data.items():
            if k not in payload:
                payload[k] = v
        try:
            await self._send_fn(payload)
        except Exception as e:
            logger.debug("web_user.send_failed type={} error={}", event.type, str(e)[:100])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_web.py -v --no-header --timeout=10`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/user_web.py tests/test_user_web.py && git commit -m "feat: add WebUser with WebSocket event push"
```

---

## Task 5: 创建 QQUser — `agent_core/user_qq.py`

**Files:**
- Create: `agent_core/user_qq.py`
- Test: `tests/test_user_qq.py`

**Interfaces:**
- Consumes: `UserBase` from Task 2, `AgentEvent` from Task 1
- Produces: `QQUser`
- Design decision: 仅 SUB_STARTED 时发送1条消息，其余静默（节省5条限制）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_qq.py
"""QQUser 测试 — 仅 SUB_STARTED 通知，其余静默。"""
import pytest
from agent_core.user_qq import QQUser
from core.event_bus import AgentEvent, AgentEventType


@pytest.mark.asyncio
async def test_qq_user_sub_started_sends_message():
    """SUB_STARTED 发送1条消息：🔄 {display}正在思考..."""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaolang",
        task_id="t1",
        data={"display_name": "小狼"},
    ))
    assert len(sent) == 1
    assert "🔄" in sent[0]
    assert "小狼" in sent[0]
    assert "思考" in sent[0]


@pytest.mark.asyncio
async def test_qq_user_sub_completed_silent():
    """SUB_COMPLETED 不发送消息（节省消息条数）。"""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_COMPLETED,
        agent="xiaoli",
        task_id="t2",
    ))
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_qq_user_tool_events_silent():
    """TOOL_* 事件不发送消息。"""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_STARTED,
        agent="xiaoke",
        task_id="t3",
        data={"tool_name": "web_search"},
    ))
    await user.deliver(AgentEvent(
        type=AgentEventType.TOOL_COMPLETED,
        agent="xiaoke",
        task_id="t3",
        data={"tool_name": "web_search"},
    ))
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_qq_user_sub_failed_silent():
    """SUB_FAILED 不发送消息（主回复会包含降级文案）。"""
    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_FAILED,
        agent="xiaolian",
        task_id="t4",
    ))
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_qq_user_reply_error_does_not_raise():
    """reply_fn 异常不中断调用方。"""
    async def broken_reply(content: str, msg_seq: int = 0) -> None:
        raise RuntimeError("qq rate limited")

    user = QQUser(reply_fn=broken_reply, msg_seq_fn=lambda: 1)
    await user.deliver(AgentEvent(
        type=AgentEventType.SUB_STARTED,
        agent="xiaoda",
        task_id="t5",
    ))
    # 无异常即通过
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_qq.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.user_qq'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/user_qq.py
"""QQ User — 仅 SUB_STARTED 通知，其余静默。

QQ Bot 消息频率封顶 5 条/轮，子代理完成/失败事件不额外消耗消息条数，
主回复本身已包含最终结果或降级文案。
"""
from __future__ import annotations

from typing import Awaitable, Callable

from loguru import logger

from core.event_bus import AgentEvent, AgentEventType
from agent_core.user_base import UserBase, AGENT_DISPLAY


class QQUser(UserBase):
    """QQ 端：仅子代理开始时发送1条通知消息。

    Args:
        reply_fn: QQ 消息回复函数，签名为 async (content: str, msg_seq: int) -> None
        msg_seq_fn: 获取下一个 msg_seq 的函数
    """

    def __init__(
        self,
        reply_fn: Callable[[str, int], Awaitable[None]],
        msg_seq_fn: Callable[[], int],
    ) -> None:
        self._reply_fn = reply_fn
        self._msg_seq_fn = msg_seq_fn

    async def deliver(self, event: AgentEvent) -> None:
        # 仅 SUB_STARTED 发送消息，其余事件静默
        if event.type != AgentEventType.SUB_STARTED:
            return
        display = event.data.get("display_name") or AGENT_DISPLAY.get(event.agent, event.agent)
        content = f"🔄 {display}正在思考..."
        try:
            await self._reply_fn(content=content, msg_seq=self._msg_seq_fn())
        except Exception as e:
            logger.debug("qq_user.reply_failed agent={} error={}", event.agent, str(e)[:100])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_user_qq.py -v --no-header --timeout=10`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/user_qq.py tests/test_user_qq.py && git commit -m "feat: add QQUser with start-only notification (5-msg limit compliant)"
```

---

## Task 6: EventBus 集成到 sub_agent_manager — emit SUB_* 事件

**Files:**
- Modify: `agent_core/sub_agent_manager.py` (L58-L144, L246-L300, L385-L464)
- Test: `tests/test_sub_agent_events.py`

**Interfaces:**
- Consumes: `event_bus`, `AgentEvent`, `AgentEventType`, `gen_task_id` from Task 1
- Produces: 3 处 dispatch 方法加 emit

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sub_agent_events.py
"""子代理 dispatch 事件发射测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.event_bus import event_bus, AgentEvent, AgentEventType


class FakeUser:
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def deliver(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_dispatch_single_emits_started_and_completed():
    """_dispatch_single_sub_agent 发射 SUB_STARTED + SUB_COMPLETED。"""
    from agent_core.sub_agent_manager import SubAgentManagerMixin

    user = FakeUser()
    event_bus.bind_user(user)
    try:
        # 创建 mixin 实例（需要 mock 依赖）
        mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
        mgr.dispatcher = MagicMock()
        mgr.dispatcher.get_agent = MagicMock(return_value=MagicMock(
            available=True, config=MagicMock(display_name="小狼")
        ))
        mgr.dispatcher.dispatch = AsyncMock(return_value="这是小狼的回复")
        mgr.context = MagicMock()
        mgr.context.current_address_term = ""
        mgr.context.add_message = AsyncMock()
        mgr._build_sub_agent_context = MagicMock(return_value="")
        mgr._bg_task_manager = MagicMock()
        mgr._bg_task_manager.run_background_tasks = MagicMock()
        mgr._voice_mode = False
        mgr._finalize_reply = MagicMock(side_effect=lambda x, **kw: x)
        mgr.security = MagicMock()
        mgr.security.is_owner = MagicMock(return_value=True)
        mgr.get_sticker_manager = MagicMock(return_value=MagicMock(available=False))

        result = await mgr._dispatch_single_sub_agent(
            target="xiaolang",
            clean_input="写个函数",
            user_id="test",
            source="test",
            session_id="s1",
            trace=MagicMock(),
        )

        # 验证事件
        types = [e.type for e in user.events]
        assert AgentEventType.SUB_STARTED in types
        assert AgentEventType.SUB_COMPLETED in types
        # 验证事件数据
        started = [e for e in user.events if e.type == AgentEventType.SUB_STARTED][0]
        assert started.agent == "xiaolang"
        assert started.data["display_name"] == "小狼"
    finally:
        event_bus.unbind_user()


@pytest.mark.asyncio
async def test_dispatch_single_emits_failed_on_exception():
    """dispatch 异常时发射 SUB_FAILED。"""
    from agent_core.sub_agent_manager import SubAgentManagerMixin

    user = FakeUser()
    event_bus.bind_user(user)
    try:
        mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
        mgr.dispatcher = MagicMock()
        mgr.dispatcher.get_agent = MagicMock(return_value=MagicMock(
            available=True, config=MagicMock(display_name="小狼")
        ))
        mgr.dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("timeout"))
        mgr.context = MagicMock()
        mgr.context.current_address_term = ""
        mgr.context.add_message = AsyncMock()
        mgr._build_sub_agent_context = MagicMock(return_value="")
        mgr._bg_task_manager = MagicMock()
        mgr._bg_task_manager.run_background_tasks = MagicMock()
        mgr._voice_mode = False
        mgr._finalize_reply = MagicMock(side_effect=lambda x, **kw: x)
        mgr.security = MagicMock()
        mgr.security.is_owner = MagicMock(return_value=True)
        mgr.get_sticker_manager = MagicMock(return_value=MagicMock(available=False))

        result = await mgr._dispatch_single_sub_agent(
            target="xiaolang",
            clean_input="写个函数",
            user_id="test",
            source="test",
            session_id="s1",
            trace=MagicMock(),
        )

        types = [e.type for e in user.events]
        assert AgentEventType.SUB_STARTED in types
        assert AgentEventType.SUB_FAILED in types
    finally:
        event_bus.unbind_user()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_sub_agent_events.py -v --no-header --timeout=10`
Expected: FAIL — 事件未发射（user.events 为空）

- [ ] **Step 3: Implement — 修改 `_dispatch_single_sub_agent`**

在 `agent_core/sub_agent_manager.py` 文件顶部新增导入：
```python
from core.event_bus import event_bus, AgentEvent, AgentEventType, gen_task_id
```

在 `_dispatch_single_sub_agent` 方法体（L58-L144）中，在 dispatch 前后加 emit。修改 L67-L74 区域：

```python
# 原代码 L67-L72:
        display_name = sub_agent.config.display_name
        trace.info("agent.chat_target_sub", target=target, input_preview=clean_input[:50])
        context_str = self._build_sub_agent_context()
        # 注入情绪标签规则...
        sub_reply = await self.dispatcher.dispatch(target, clean_input, context=context_str, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term, extra_system_prompt=_SUB_AGENT_EMOTION_RULE)
        if sub_reply is None:
            sub_reply = f"{display_name}现在有点累了...等会儿再来吧！💤"

# 改为:
        display_name = sub_agent.config.display_name
        task_id = gen_task_id(target)
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent=target,
            task_id=task_id,
            data={"display_name": display_name, "input_preview": clean_input[:50]},
        ))
        trace.info("agent.chat_target_sub", target=target, input_preview=clean_input[:50])
        context_str = self._build_sub_agent_context()
        # 注入情绪标签规则：@ 直接对话模式下，子 Agent 回复需带 [emotion:xxx] 标签
        # 以触发专属表情包系统（delegate_task 工具调用不注入，保持"不加标签"）
        try:
            sub_reply = await self.dispatcher.dispatch(target, clean_input, context=context_str, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term, extra_system_prompt=_SUB_AGENT_EMOTION_RULE)
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_COMPLETED,
                agent=target,
                task_id=task_id,
                data={"reply_preview": (sub_reply or "")[:100]},
            ))
        except Exception as dispatch_err:
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_FAILED,
                agent=target,
                task_id=task_id,
                data={"error": str(dispatch_err)[:200]},
            ))
            sub_reply = None
        if sub_reply is None:
            sub_reply = f"{display_name}现在有点累了...等会儿再来吧！💤"
```

- [ ] **Step 4: Implement — 修改 `_parallel_run_one`**

在 `_parallel_run_one` 方法（L246-L300）中，dispatch 前后加 emit。修改 L269-L273 区域：

```python
# 在 L269 (try:) 之前加:
        task_id = gen_task_id(t)
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent=t,
            task_id=task_id,
            data={"mode": "parallel", "input_preview": sub_task[:50]},
        ))

# 在 L270-L273 的 try 块内 dispatch 后加:
        try:
            reply = await asyncio.wait_for(
                self.dispatcher.dispatch(t, sub_task, context=sub_context, status_callback=None, address_term=self.context.current_address_term),
                timeout=180,
            )
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_COMPLETED,
                agent=t,
                task_id=task_id,
                data={"reply_preview": (reply or "")[:100]},
            ))
# 在 except TimeoutError 和 except Exception 中加:
        except TimeoutError:
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_FAILED,
                agent=t,
                task_id=task_id,
                data={"error": "timeout"},
            ))
            return {"agent": t, "display_name": display_name,
                    "reply": f"{display_name}处理超时", "error": True}
        except Exception as e:
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_FAILED,
                agent=t,
                task_id=task_id,
                data={"error": str(e)[:200]},
            ))
            # ... 原有错误分类逻辑 ...
```

- [ ] **Step 5: Implement — 修改 `delegate_to_agent`**

在 `delegate_to_agent` 方法（L385-L464）中，dispatch 前后加 emit。修改 L432-L434 区域：

```python
# 在 L432 (result = await asyncio.wait_for...) 之前加:
        task_id = gen_task_id(name)
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_STARTED,
            agent=name,
            task_id=task_id,
            data={"mode": "delegate", "task_preview": task[:80]},
        ))

# L432-L434 的 dispatch 后加:
        result = await asyncio.wait_for(self.dispatcher.dispatch(
            name, task, context=context,
            status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term), timeout=180)
        await event_bus.emit(AgentEvent(
            type=AgentEventType.SUB_COMPLETED if result else AgentEventType.SUB_FAILED,
            agent=name,
            task_id=task_id,
            data={"result_preview": (result or "")[:100]},
        ))
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_sub_agent_events.py -v --no-header --timeout=10`
Expected: 2 passed

- [ ] **Step 7: Run regression to verify no breakage**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/ -q --no-header --timeout=60 -x 2>&1 | tail -5`
Expected: 0 failures

- [ ] **Step 8: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/sub_agent_manager.py tests/test_sub_agent_events.py && git commit -m "feat: emit SUB_STARTED/COMPLETED/FAILED events in 3 dispatch methods"
```

---

## Task 7: 工具事件迁移到 EventBus — `tool_engine/tool_call_handler.py`

**Files:**
- Modify: `tool_engine/tool_call_handler.py` (L120-L143)
- Test: `tests/test_tool_event_migration.py`

**Interfaces:**
- Consumes: `event_bus`, `AgentEvent`, `AgentEventType` from Task 1
- Produces: `_notify_tool_status` 改为 emit TOOL_* 事件到 EventBus（保留 status_callback 兜底）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_event_migration.py
"""工具事件迁移到 EventBus 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.event_bus import event_bus, AgentEvent, AgentEventType


class FakeUser:
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def deliver(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_notify_tool_status_started_emits_event():
    """_notify_tool_status(stage='started') 发射 TOOL_STARTED 事件。"""
    from tool_engine.tool_call_handler import ToolCallHandler

    user = FakeUser()
    event_bus.bind_user(user)
    try:
        handler = ToolCallHandler.__new__(ToolCallHandler)
        handler._status_callback = None
        handler._agent_name = "xiaoke"

        await handler._notify_tool_status("web_search", "started")

        tool_events = [e for e in user.events if e.type == AgentEventType.TOOL_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].data["tool_name"] == "web_search"
    finally:
        event_bus.unbind_user()


@pytest.mark.asyncio
async def test_notify_tool_status_completed_emits_event():
    """_notify_tool_status(stage='completed') 发射 TOOL_COMPLETED 事件。"""
    from tool_engine.tool_call_handler import ToolCallHandler

    user = FakeUser()
    event_bus.bind_user(user)
    try:
        handler = ToolCallHandler.__new__(ToolCallHandler)
        handler._status_callback = None
        handler._agent_name = "xiaoke"

        await handler._notify_tool_status("web_search", "completed", "found 3 results")

        tool_events = [e for e in user.events if e.type == AgentEventType.TOOL_COMPLETED]
        assert len(tool_events) == 1
        assert tool_events[0].data["tool_name"] == "web_search"
    finally:
        event_bus.unbind_user()


@pytest.mark.asyncio
async def test_notify_tool_status_failed_emits_event():
    """_notify_tool_status(stage='failed') 发射 TOOL_FAILED 事件。"""
    from tool_engine.tool_call_handler import ToolCallHandler

    user = FakeUser()
    event_bus.bind_user(user)
    try:
        handler = ToolCallHandler.__new__(ToolCallHandler)
        handler._status_callback = None
        handler._agent_name = "xiaoke"

        await handler._notify_tool_status("web_search", "failed", "timeout")

        tool_events = [e for e in user.events if e.type == AgentEventType.TOOL_FAILED]
        assert len(tool_events) == 1
    finally:
        event_bus.unbind_user()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_tool_event_migration.py -v --no-header --timeout=10`
Expected: FAIL — 事件未发射

- [ ] **Step 3: Implement — 修改 `_notify_tool_status`**

在 `tool_engine/tool_call_handler.py` 文件顶部新增导入：
```python
from core.event_bus import event_bus, AgentEvent, AgentEventType
```

修改 `_notify_tool_status` 方法（L120-L143）：

```python
    async def _notify_tool_status(self, tool_name: str, stage: str, detail: str = "") -> None:
        """推送工具调用的中间状态 — 通过 EventBus 发射 TOOL_* 事件。

        Args:
            tool_name: 工具名称，如 "web_search"、"memory_search"
            stage: "started" / "completed" / "failed"
            detail: 详细信息
        """
        from config import STREAM_TOOL_STATUS
        if not STREAM_TOOL_STATUS:
            return

        # EventBus 事件发射（统一事件通道）
        stage_to_type = {
            "started": AgentEventType.TOOL_STARTED,
            "completed": AgentEventType.TOOL_COMPLETED,
            "failed": AgentEventType.TOOL_FAILED,
        }
        event_type = stage_to_type.get(stage)
        if event_type:
            await event_bus.emit(AgentEvent(
                type=event_type,
                agent=getattr(self, "_agent_name", ""),
                task_id=getattr(self, "_task_id", ""),
                data={"tool_name": tool_name, "detail": detail[:100] if detail else ""},
            ))

        # 保留 status_callback 兜底（向后兼容）
        if not self._status_callback:
            return
        stage_labels = {"started": "正在调用", "completed": "完成", "failed": "失败"}
        label = stage_labels.get(stage, stage)
        display = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
        try:
            await self._status_callback({
                "type": "tool_status",
                "tool": tool_name,
                "stage": stage,
                "label": f"{label} {display}...",
                "detail": detail[:100] if detail else "",
            })
        except Exception as e:
            logger.debug(f"tool_status_push_failed: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_tool_event_migration.py -v --no-header --timeout=10`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add tool_engine/tool_call_handler.py tests/test_tool_event_migration.py && git commit -m "feat: migrate tool status events to EventBus (keep status_callback fallback)"
```

---

## Task 8: 传输层绑定 User — QQ/Web/CLI

**Files:**
- Modify: `qq_bot_adapter.py` (L570-L590)
- Modify: `web/ws_hub.py` (L460-L490)
- Test: `tests/test_transport_user_binding.py`

**Interfaces:**
- Consumes: `QQUser` from Task 5, `WebUser` from Task 4, `CLIUser` from Task 3
- Produces: session 开始 bind_user，结束 unbind_user

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transport_user_binding.py
"""传输层 User 绑定测试。"""
import pytest
from core.event_bus import event_bus


def test_event_bus_has_no_user_after_unbind():
    """unbind 后 bound_user 为 None。"""
    event_bus.unbind_user()
    assert event_bus.bound_user is None


@pytest.mark.asyncio
async def test_qq_adapter_binds_qq_user():
    """QQ 适配器在 process 前 bind QQUser，后 unbind。"""
    # 这个测试验证 QQUser 被正确绑定
    # 由于 QQ 适配器涉及较多 mock，这里只验证 event_bus 状态
    from agent_core.user_qq import QQUser

    sent: list[str] = []

    async def fake_reply(content: str, msg_seq: int = 0) -> None:
        sent.append(content)

    user = QQUser(reply_fn=fake_reply, msg_seq_fn=lambda: 1)
    event_bus.bind_user(user)
    assert isinstance(event_bus.bound_user, QQUser)
    event_bus.unbind_user()
    assert event_bus.bound_user is None


@pytest.mark.asyncio
async def test_web_adapter_binds_web_user():
    """Web 适配器 bind WebUser。"""
    from agent_core.user_web import WebUser

    async def fake_send(event: dict) -> None:
        pass

    user = WebUser(send_fn=fake_send)
    event_bus.bind_user(user)
    assert isinstance(event_bus.bound_user, WebUser)
    event_bus.unbind_user()
    assert event_bus.bound_user is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_transport_user_binding.py -v --no-header --timeout=10`
Expected: PASS（测试本身不依赖修改，验证现有行为）— 如果 FAIL 则有回归

- [ ] **Step 3: Implement — 修改 `qq_bot_adapter.py`**

在 `qq_bot_adapter.py` 文件顶部新增导入：
```python
from core.event_bus import event_bus
from agent_core.user_qq import QQUser
```

在 `_handle_message` 方法中（约 L570-L590），`status_notify` 定义之后、`agent.process` 调用之前，绑定 QQUser：

```python
            async def status_notify(msg) -> None:
                # tool_call_handler._notify_tool_status 传入 dict 类型（工具状态），
                # 不应直接发送到 QQ（会导致 content 类型错误），静默处理
                if isinstance(msg, dict):
                    return
                if not isinstance(msg, str) or not msg:
                    return
                await message.reply(content=msg, msg_seq=_next_msg_seq())

            # ← 新增：绑定 QQUser 到 EventBus
            qq_user = QQUser(
                reply_fn=lambda content, msg_seq: message.reply(content=content, msg_seq=msg_seq),
                msg_seq_fn=_next_msg_seq,
            )
            event_bus.bind_user(qq_user)
            try:
                result = await asyncio.wait_for(
                    self.agent.process(user_input, user_id=user_id, source="qq_c2c",
                                      user_openid=user_openid, session_id=session_id,
                                      status_callback=status_notify,
                                      image_data=image_data if image_data else None,
                                      is_master=is_master),
                    timeout=120,
                )
            finally:
                event_bus.unbind_user()
```

- [ ] **Step 4: Implement — 修改 `web/ws_hub.py`**

在 `web/ws_hub.py` 的 `on_status` 函数定义附近（约 L468），新增 WebUser 绑定：

```python
    # ← 新增：绑定 WebUser 到 EventBus
    from core.event_bus import event_bus
    from agent_core.user_web import WebUser

    async def _ws_send(event: dict) -> None:
        await manager.send_to(conn_id, event)

    web_user = WebUser(send_fn=_ws_send)
    event_bus.bind_user(web_user)
    try:
        # ... 原有 agent.process 调用 ...
    finally:
        event_bus.unbind_user()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_transport_user_binding.py -v --no-header --timeout=10`
Expected: 3 passed

- [ ] **Step 6: Run regression**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/ -q --no-header --timeout=60 -x 2>&1 | tail -5`
Expected: 0 failures

- [ ] **Step 7: Commit**

```bash
cd /home/orangepi/ai-agent && git add qq_bot_adapter.py web/ws_hub.py tests/test_transport_user_binding.py && git commit -m "feat: bind QQUser/WebUser to EventBus in transport layer"
```

---

## Task 9: CancelToken — `core/cancel_token.py`

**Files:**
- Create: `core/cancel_token.py`
- Modify: `agent_core/sub_agent_manager.py` (3 处 dispatch 加 cancel 检查)
- Test: `tests/test_cancel_token.py`

**Interfaces:**
- Consumes: `event_bus`, `AgentEventType.SUB_CANCELLED` from Task 1
- Produces: `CancelToken`, `CancellationError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cancel_token.py
"""CancelToken 测试 — 超时自动取消 + 主动取消。"""
import asyncio
import pytest
from core.cancel_token import CancelToken, CancellationError


@pytest.mark.asyncio
async def test_cancel_token_not_cancelled_by_default():
    """新建的 CancelToken 默认未取消。"""
    token = CancelToken(timeout=10.0)
    assert not token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_token_explicit_cancel():
    """主动 cancel() 后 is_cancelled=True。"""
    token = CancelToken(timeout=10.0)
    token.cancel("manual")
    assert token.is_cancelled
    assert token.reason == "manual"


@pytest.mark.asyncio
async def test_cancel_token_timeout_auto_cancel():
    """超时后自动取消。"""
    token = CancelToken(timeout=0.1)
    await asyncio.sleep(0.2)
    assert token.is_cancelled
    assert "timeout" in token.reason


@pytest.mark.asyncio
async def test_cancel_token_check_raises_when_cancelled():
    """check() 在已取消时抛出 CancellationError。"""
    token = CancelToken(timeout=10.0)
    token.cancel("manual")
    with pytest.raises(CancellationError):
        token.check()


@pytest.mark.asyncio
async def test_cancel_token_check_passes_when_not_cancelled():
    """check() 在未取消时不抛异常。"""
    token = CancelToken(timeout=10.0)
    token.check()  # 无异常即通过


@pytest.mark.asyncio
async def test_cancel_token_with_timeout_none_never_auto_cancel():
    """timeout=None 时永不自动取消。"""
    token = CancelToken(timeout=None)
    await asyncio.sleep(0.05)
    assert not token.is_cancelled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_cancel_token.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.cancel_token'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/cancel_token.py
"""CancelToken — 协程取消令牌，支持超时自动取消 + 主动取消。

使用方式：
    token = CancelToken(timeout=60.0)
    try:
        # 在 dispatch 前检查
        token.check()
        result = await some_long_running_task()
        token.check()  # 在关键节点检查
    except CancellationError:
        # 处理取消
        ...
    finally:
        token.cleanup()

    # 主动取消（主 Agent 调用）
    token.cancel("agent_request")
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger


class CancellationError(Exception):
    """任务被取消时抛出的异常。"""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(f"task cancelled: {reason}")


class CancelToken:
    """协程取消令牌。

    Args:
        timeout: 超时秒数，None 表示永不超时
    """

    def __init__(self, timeout: Optional[float] = 60.0) -> None:
        self._cancelled = False
        self._reason = ""
        self._timeout = timeout
        self._created_at = time.monotonic()
        self._timer_task: asyncio.Task | None = None
        if timeout is not None and timeout > 0:
            self._timer_task = asyncio.create_task(self._timeout_watch())

    async def _timeout_watch(self) -> None:
        """后台监控超时。"""
        try:
            await asyncio.sleep(self._timeout)
            if not self._cancelled:
                self._cancelled = True
                self._reason = f"timeout({self._timeout}s)"
                logger.info("cancel_token.timeout_cancelled timeout={}s", self._timeout)
        except asyncio.CancelledError:
            pass

    @property
    def is_cancelled(self) -> bool:
        """是否已取消。"""
        # 如果没有 timer task（timeout=None），检查 elapsed
        if self._timeout is not None and self._timeout > 0:
            if not self._cancelled and time.monotonic() - self._created_at > self._timeout:
                self._cancelled = True
                self._reason = f"timeout({self._timeout}s)"
        return self._cancelled

    @property
    def reason(self) -> str:
        """取消原因。"""
        return self._reason

    def cancel(self, reason: str = "manual") -> None:
        """主动取消。"""
        if not self._cancelled:
            self._cancelled = True
            self._reason = reason
            logger.info("cancel_token.cancelled reason={}", reason)

    def check(self) -> None:
        """检查是否已取消，已取消则抛出 CancellationError。"""
        if self.is_cancelled:
            raise CancellationError(self._reason)

    def cleanup(self) -> None:
        """清理资源（取消 timer task）。"""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_cancel_token.py -v --no-header --timeout=10`
Expected: 6 passed

- [ ] **Step 5: Integrate CancelToken into sub_agent_manager**

在 `agent_core/sub_agent_manager.py` 的 `_dispatch_single_sub_agent` 中，包裹 dispatch 调用：

```python
from core.cancel_token import CancelToken, CancellationError

# 在 _dispatch_single_sub_agent 的 try 块内:
        try:
            token = CancelToken(timeout=180.0)
            token.check()
            sub_reply = await self.dispatcher.dispatch(target, clean_input, context=context_str, status_callback=_ctx.status_callback if _ctx else None, address_term=self.context.current_address_term, extra_system_prompt=_SUB_AGENT_EMOTION_RULE)
            token.check()
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_COMPLETED,
                agent=target,
                task_id=task_id,
                data={"reply_preview": (sub_reply or "")[:100]},
            ))
        except CancellationError:
            await event_bus.emit(AgentEvent(
                type=AgentEventType.SUB_CANCELLED,
                agent=target,
                task_id=task_id,
                data={"reason": token.reason},
            ))
            sub_reply = f"{display_name}被取消了"
        except Exception as dispatch_err:
            # ... 原有错误处理 ...
```

- [ ] **Step 6: Run regression**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/ -q --no-header --timeout=60 -x 2>&1 | tail -5`
Expected: 0 failures

- [ ] **Step 7: Commit**

```bash
cd /home/orangepi/ai-agent && git add core/cancel_token.py tests/test_cancel_token.py agent_core/sub_agent_manager.py && git commit -m "feat: add CancelToken with timeout + manual cancel support"
```

---

## Task 10: 修复 BeliefRouter bug — `decide()` → `select_agent()`

**Files:**
- Modify: `core/router_engine.py` (L201)
- Test: `tests/test_belief_router_bugfix.py`

**Interfaces:**
- Consumes: `BeliefRouter` from `belief_router.py`
- Produces: 修复后的 `decide()` 方法

- [ ] **Step 1: Write the failing test**

```python
# tests/test_belief_router_bugfix.py
"""BeliefRouter bug 修复测试 — decide() 应调用 select_agent()。"""
import os
import pytest
from unittest.mock import MagicMock, patch
from core.router_engine import RouterEngine


def test_decide_calls_select_agent_not_decide():
    """RouterEngine.decide() 应调用 BeliefRouter.select_agent()，而非不存在的 decide()。"""
    # 创建 mock BeliefRouter
    belief_router = MagicMock()
    belief_router.select_agent = MagicMock(return_value="xiaolang")

    engine = RouterEngine(belief_router=belief_router)
    # 启用 belief 路由
    with patch.dict(os.environ, {"ROUTER_ENGINE": "new"}):
        engine._use_belief = True
        # 调用 decide，传入不会匹配 @mention/否定/自指/语音/关键词的输入
        decision = engine.decide("帮我写个Python函数", user_id="test")

    # 验证 select_agent 被调用
    belief_router.select_agent.assert_called_once()
    # 验证没有调用 decide
    belief_router.decide.assert_not_called() if hasattr(belief_router, 'decide') else None
    # 验证路由结果
    assert "xiaolang" in decision.agent_names


def test_decide_belief_fallback_on_exception():
    """BeliefRouter.select_agent() 异常时降级到关键词匹配。"""
    belief_router = MagicMock()
    belief_router.select_agent = MagicMock(side_effect=RuntimeError("broken"))

    engine = RouterEngine(belief_router=belief_router)
    with patch.dict(os.environ, {"ROUTER_ENGINE": "new"}):
        engine._use_belief = True
        # "写代码" 应匹配关键词 → xiaolang
        decision = engine.decide("帮我写代码", user_id="test")

    # 验证降级到关键词匹配
    assert "xiaolang" in decision.agent_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_belief_router_bugfix.py -v --no-header --timeout=10`
Expected: FAIL — `select_agent` 未被调用（因为代码调用的是 `decide()`）

- [ ] **Step 3: Implement — 修复 bug**

修改 `core/router_engine.py` L201：

```python
# 原代码 L201:
                belief_target = self._belief_router.decide(user_input, user_id)

# 改为:
                belief_target = self._belief_router.select_agent()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_belief_router_bugfix.py -v --no-header --timeout=10`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add core/router_engine.py tests/test_belief_router_bugfix.py && git commit -m "fix: RouterEngine calls BeliefRouter.select_agent() instead of non-existent decide()"
```

---

## Task 11: BeliefRouter 反馈回路 — dispatch 后调用 update_belief

**Files:**
- Modify: `agent_core/sub_agent_manager.py` (3 处 dispatch 后加 update_belief)
- Modify: `core/router_engine.py` (暴露 belief_router 引用)
- Test: `tests/test_belief_feedback.py`

**Interfaces:**
- Consumes: `BeliefRouter.update_belief(agent_name, success)` from `belief_router.py`
- Produces: dispatch 成功/失败后更新信念

- [ ] **Step 1: Write the failing test**

```python
# tests/test_belief_feedback.py
"""BeliefRouter 反馈回路测试 — dispatch 后更新信念。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.event_bus import event_bus


@pytest.mark.asyncio
async def test_dispatch_success_updates_belief():
    """子代理 dispatch 成功后调用 update_belief(success=True)。"""
    from agent_core.sub_agent_manager import SubAgentManagerMixin

    belief_router = MagicMock()
    belief_router.update_belief = MagicMock()

    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    mgr.dispatcher = MagicMock()
    mgr.dispatcher.get_agent = MagicMock(return_value=MagicMock(
        available=True, config=MagicMock(display_name="小狼")
    ))
    mgr.dispatcher.dispatch = AsyncMock(return_value="这是小狼的回复")
    mgr.context = MagicMock()
    mgr.context.current_address_term = ""
    mgr.context.add_message = AsyncMock()
    mgr.context.belief_router = belief_router  # 注入 belief_router
    mgr._build_sub_agent_context = MagicMock(return_value="")
    mgr._bg_task_manager = MagicMock()
    mgr._bg_task_manager.run_background_tasks = MagicMock()
    mgr._voice_mode = False
    mgr._finalize_reply = MagicMock(side_effect=lambda x, **kw: x)
    mgr.security = MagicMock()
    mgr.security.is_owner = MagicMock(return_value=True)
    mgr.get_sticker_manager = MagicMock(return_value=MagicMock(available=False))

    event_bus.unbind_user()
    await mgr._dispatch_single_sub_agent(
        target="xiaolang",
        clean_input="写个函数",
        user_id="test",
        source="test",
        session_id="s1",
        trace=MagicMock(),
    )

    # 验证 update_belief 被调用，success=True
    belief_router.update_belief.assert_called_once_with("xiaolang", True)


@pytest.mark.asyncio
async def test_dispatch_failure_updates_belief():
    """子代理 dispatch 异常后调用 update_belief(success=False)。"""
    from agent_core.sub_agent_manager import SubAgentManagerMixin

    belief_router = MagicMock()
    belief_router.update_belief = MagicMock()

    mgr = SubAgentManagerMixin.__new__(SubAgentManagerMixin)
    mgr.dispatcher = MagicMock()
    mgr.dispatcher.get_agent = MagicMock(return_value=MagicMock(
        available=True, config=MagicMock(display_name="小狼")
    ))
    mgr.dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("timeout"))
    mgr.context = MagicMock()
    mgr.context.current_address_term = ""
    mgr.context.add_message = AsyncMock()
    mgr.context.belief_router = belief_router
    mgr._build_sub_agent_context = MagicMock(return_value="")
    mgr._bg_task_manager = MagicMock()
    mgr._bg_task_manager.run_background_tasks = MagicMock()
    mgr._voice_mode = False
    mgr._finalize_reply = MagicMock(side_effect=lambda x, **kw: x)
    mgr.security = MagicMock()
    mgr.security.is_owner = MagicMock(return_value=True)
    mgr.get_sticker_manager = MagicMock(return_value=MagicMock(available=False))

    event_bus.unbind_user()
    await mgr._dispatch_single_sub_agent(
        target="xiaolang",
        clean_input="写个函数",
        user_id="test",
        source="test",
        session_id="s1",
        trace=MagicMock(),
    )

    # 验证 update_belief 被调用，success=False
    belief_router.update_belief.assert_called_once_with("xiaolang", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_belief_feedback.py -v --no-header --timeout=10`
Expected: FAIL — `update_belief` 未被调用

- [ ] **Step 3: Implement — 在 sub_agent_manager 中加反馈回路**

在 `agent_core/sub_agent_manager.py` 的 `_dispatch_single_sub_agent` 方法中，SUB_COMPLETED emit 之后加：

```python
# 在 SUB_COMPLETED emit 之后加:
        # BeliefRouter 反馈回路：非空回复 = 成功
        belief_router = getattr(self.context, "belief_router", None)
        if belief_router:
            try:
                belief_router.update_belief(target, bool(sub_reply and sub_reply.strip()))
            except Exception:
                pass  # 信念更新失败不影响主流程
```

在 SUB_FAILED emit 之后加：
```python
        # BeliefRouter 反馈回路：异常 = 失败
        belief_router = getattr(self.context, "belief_router", None)
        if belief_router:
            try:
                belief_router.update_belief(target, False)
            except Exception:
                pass
```

同样在 `_parallel_run_one` 和 `delegate_to_agent` 中也加 `update_belief` 调用。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_belief_feedback.py -v --no-header --timeout=10`
Expected: 2 passed

- [ ] **Step 5: Run regression**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/ -q --no-header --timeout=60 -x 2>&1 | tail -5`
Expected: 0 failures

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/sub_agent_manager.py tests/test_belief_feedback.py && git commit -m "feat: add BeliefRouter feedback loop - update_belief after dispatch"
```

---

## Task 12: SharedBlackboard SQLite 背板 — `agent_core/shared_blackboard_db.py`

**Files:**
- Create: `agent_core/shared_blackboard_db.py`
- Modify: `agent_core/shared_blackboard.py` (增加 SQLite 背板选项)
- Test: `tests/test_shared_blackboard_db.py`

**Interfaces:**
- Consumes: `SharedBlackboard` API from `agent_core/shared_blackboard.py`
- Produces: `SharedBlackboardDB` — SQLite 背板黑板，跨进程安全

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_blackboard_db.py
"""SharedBlackboardDB 测试 — SQLite 背板跨进程共享。"""
import asyncio
import tempfile
import os
import pytest
from agent_core.shared_blackboard_db import SharedBlackboardDB


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_blackboard.db")


@pytest.mark.asyncio
async def test_db_blackboard_put_and_get(db_path):
    """写入后能读取。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "value1", agent_name="xiaolang")
    result = await bb.get("key1")
    assert result == "value1"


@pytest.mark.asyncio
async def test_db_blackboard_get_nonexistent(db_path):
    """不存在的 key 返回 None。"""
    bb = SharedBlackboardDB(db_path=db_path)
    result = await bb.get("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_db_blackboard_ttl_expiry(db_path):
    """TTL 过期后返回 None。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "value1", agent_name="xiaolang", ttl=0.1)
    await asyncio.sleep(0.2)
    result = await bb.get("key1")
    assert result is None


@pytest.mark.asyncio
async def test_db_blackboard_cross_process_share(db_path):
    """两个实例共享同一 DB 文件 — 模拟跨进程。"""
    bb1 = SharedBlackboardDB(db_path=db_path)
    bb2 = SharedBlackboardDB(db_path=db_path)
    await bb1.put("shared_key", "shared_value", agent_name="xiaoli")
    # bb2 能读到 bb1 写入的数据
    result = await bb2.get("shared_key")
    assert result == "shared_value"


@pytest.mark.asyncio
async def test_db_blackboard_get_with_meta(db_path):
    """get_with_meta 返回值和写入者。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "value1", agent_name="xiaoke")
    meta = await bb.get_with_meta("key1")
    assert meta is not None
    assert meta["value"] == "value1"
    assert meta["agent_name"] == "xiaoke"


@pytest.mark.asyncio
async def test_db_blackboard_keys(db_path):
    """keys 返回所有未过期的 key。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("key1", "v1", agent_name="a")
    await bb.put("key2", "v2", agent_name="b")
    await bb.put("prefix_key3", "v3", agent_name="c")
    all_keys = await bb.keys()
    assert set(all_keys) >= {"key1", "key2", "prefix_key3"}
    prefix_keys = await bb.keys(prefix="prefix_")
    assert prefix_keys == ["prefix_key3"]


@pytest.mark.asyncio
async def test_db_blackboard_cleanup_expired(db_path):
    """cleanup_expired 清理过期条目。"""
    bb = SharedBlackboardDB(db_path=db_path)
    await bb.put("expired", "v", agent_name="a", ttl=0.1)
    await bb.put("permanent", "v", agent_name="b", ttl=3600)
    await asyncio.sleep(0.2)
    cleaned = await bb.cleanup_expired()
    assert cleaned == 1
    # 过期的已清理
    assert await bb.get("expired") is None
    # 未过期的保留
    assert await bb.get("permanent") == "v"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_shared_blackboard_db.py -v --no-header --timeout=10`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.shared_blackboard_db'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/shared_blackboard_db.py
"""SharedBlackboardDB — SQLite 背板黑板，支持跨进程共享。

与 SharedBlackboard（asyncio.Lock 单进程）不同，SharedBlackboardDB 使用 SQLite
WAL 模式作为背板，多进程/多 worker 可安全共享数据。

适用场景：
- Web 多 worker 部署
- CLI + QQ Bot 同时运行
- 任何需要跨进程共享子代理产出的场景
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any

from loguru import logger


class SharedBlackboardDB:
    """SQLite 背板黑板 — 跨进程安全。

    Args:
        db_path: SQLite 数据库文件路径
        default_ttl: 默认 TTL（秒）
    """

    def __init__(self, db_path: str, default_ttl: float = 600.0) -> None:
        self._db_path = db_path
        self._default_ttl = default_ttl
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表。"""
        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS blackboard (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                agent_name TEXT NOT NULL DEFAULT '',
                expire_at REAL,
                created_at REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blackboard_expire ON blackboard(expire_at)")
            conn.commit()
        except Exception as e:
            logger.warning("blackboard_db.init_failed error={}", e)
        finally:
            if conn:
                conn.close()

    def _serialize(self, value: Any) -> str:
        """序列化值为 JSON 字符串。"""
        return json.dumps(value, ensure_ascii=False, default=str)

    def _deserialize(self, raw: str) -> Any:
        """反序列化 JSON 字符串。"""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    async def put(self, key: str, value: Any, agent_name: str = "",
                  ttl: float | None = None) -> None:
        """写入 key-value，记录写入者。"""
        async with self._lock:
            effective_ttl = self._default_ttl if ttl is None else ttl
            expire_at = time.time() + effective_ttl if effective_ttl > 0 else None
            raw_value = self._serialize(value)
            now = time.time()

            def _do() -> None:
                conn = None
                try:
                    conn = sqlite3.connect(self._db_path)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "INSERT OR REPLACE INTO blackboard (key, value, agent_name, expire_at, created_at) VALUES (?, ?, ?, ?, ?)",
                        (key, raw_value, agent_name, expire_at, now)
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("blackboard_db.put_failed key={} error={}", key, e)
                finally:
                    if conn:
                        conn.close()

            await asyncio.get_event_loop().run_in_executor(None, _do)
            logger.debug("blackboard_db.put key={} agent={} ttl={}", key, agent_name, effective_ttl)

    async def get(self, key: str) -> Any | None:
        """读取 key 的值；过期则清理并返回 None。"""
        async with self._lock:
            def _do() -> Any | None:
                conn = None
                try:
                    conn = sqlite3.connect(self._db_path)
                    conn.execute("PRAGMA journal_mode=WAL")
                    cur = conn.execute(
                        "SELECT value, expire_at FROM blackboard WHERE key = ?", (key,)
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    raw_value, expire_at = row
                    if expire_at is not None and time.time() > expire_at:
                        conn.execute("DELETE FROM blackboard WHERE key = ?", (key,))
                        conn.commit()
                        return None
                    return self._deserialize(raw_value)
                except Exception as e:
                    logger.warning("blackboard_db.get_failed key={} error={}", key, e)
                    return None
                finally:
                    if conn:
                        conn.close()

            return await asyncio.get_event_loop().run_in_executor(None, _do)

    async def get_with_meta(self, key: str) -> dict | None:
        """读取 key 的值及元信息。"""
        async with self._lock:
            def _do() -> dict | None:
                conn = None
                try:
                    conn = sqlite3.connect(self._db_path)
                    conn.execute("PRAGMA journal_mode=WAL")
                    cur = conn.execute(
                        "SELECT value, agent_name, expire_at FROM blackboard WHERE key = ?", (key,)
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    raw_value, agent_name, expire_at = row
                    if expire_at is not None and time.time() > expire_at:
                        conn.execute("DELETE FROM blackboard WHERE key = ?", (key,))
                        conn.commit()
                        return None
                    return {"value": self._deserialize(raw_value), "agent_name": agent_name}
                except Exception as e:
                    logger.warning("blackboard_db.get_meta_failed key={} error={}", key, e)
                    return None
                finally:
                    if conn:
                        conn.close()

            return await asyncio.get_event_loop().run_in_executor(None, _do)

    async def keys(self, prefix: str = "") -> list[str]:
        """返回所有未过期的 key（可按前缀过滤）。"""
        async with self._lock:
            def _do() -> list[str]:
                conn = None
                try:
                    conn = sqlite3.connect(self._db_path)
                    conn.execute("PRAGMA journal_mode=WAL")
                    now = time.time()
                    if prefix:
                        cur = conn.execute(
                            "SELECT key FROM blackboard WHERE key LIKE ? AND (expire_at IS NULL OR expire_at > ?)",
                            (prefix + "%", now)
                        )
                    else:
                        cur = conn.execute(
                            "SELECT key FROM blackboard WHERE expire_at IS NULL OR expire_at > ?",
                            (now,)
                        )
                    return [row[0] for row in cur.fetchall()]
                except Exception as e:
                    logger.warning("blackboard_db.keys_failed error={}", e)
                    return []
                finally:
                    if conn:
                        conn.close()

            return await asyncio.get_event_loop().run_in_executor(None, _do)

    async def cleanup_expired(self) -> int:
        """清理所有过期条目。"""
        async with self._lock:
            def _do() -> int:
                conn = None
                try:
                    conn = sqlite3.connect(self._db_path)
                    conn.execute("PRAGMA journal_mode=WAL")
                    now = time.time()
                    cur = conn.execute(
                        "DELETE FROM blackboard WHERE expire_at IS NOT NULL AND expire_at < ?",
                        (now,)
                    )
                    conn.commit()
                    return cur.rowcount
                except Exception as e:
                    logger.warning("blackboard_db.cleanup_failed error={}", e)
                    return 0
                finally:
                    if conn:
                        conn.close()

            cleaned = await asyncio.get_event_loop().run_in_executor(None, _do)
            if cleaned:
                logger.debug("blackboard_db.cleanup count={}", cleaned)
            return cleaned
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/orangepi/ai-agent && python3 -m pytest tests/test_shared_blackboard_db.py -v --no-header --timeout=15`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/ai-agent && git add agent_core/shared_blackboard_db.py tests/test_shared_blackboard_db.py && git commit -m "feat: add SharedBlackboardDB with SQLite WAL backing for cross-process sharing"
```

---

## Self-Review

### Spec coverage
- ✅ 优化1 EventBus：Task 1-5 创建 EventBus + UserBase + CLIUser/WebUser/QQUser
- ✅ 优化2 流式工具透明化：Task 7 迁移 _notify_tool_status 到 EventBus
- ✅ 优化3 CancelToken：Task 9 创建 CancelToken + 集成到 sub_agent_manager
- ✅ 优化4 RouterEngine + BeliefRouter：Task 10 修复 bug + Task 11 反馈回路
- ✅ 优化5 SharedBlackboard DB 背板：Task 12 创建 SharedBlackboardDB
- ✅ 渠道适配策略：Task 8 传输层绑定 User
- ✅ QQ 5条限制：Task 5 QQUser 仅开始时通知

### Placeholder scan
- 无 TBD/TODO，所有步骤都有完整代码
- 无 "similar to Task N"，每个任务代码完整

### Type consistency
- `AgentEvent` 在 Task 1 定义，Task 2-7 使用一致
- `AgentEventType` 枚举值在所有任务中一致
- `UserBase.deliver(event: AgentEvent)` 签名在 Task 2-5 一致
- `event_bus.emit(AgentEvent(...))` 调用模式在 Task 6-7 一致
- `CancelToken.check()` / `CancelToken.cancel()` 在 Task 9 一致
- `BeliefRouter.update_belief(agent_name, success)` 在 Task 11 与现有 belief_router.py 一致
- `SharedBlackboardDB.put/get/keys/cleanup_expired` 与 Task 12 测试一致
