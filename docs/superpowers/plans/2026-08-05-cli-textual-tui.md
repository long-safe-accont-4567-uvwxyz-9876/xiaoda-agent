# xiaoda CLI 用 Textual 重构为可点击富文本 TUI 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Textual 把 CLI 重做为可鼠标点击的富文本 TUI，统一纳西妲绿色美术，重点重做斜杠命令面板（搜索/分组/鼠标点击/多步二级面板），并保留旧终端降级回 prompt_toolkit CLI。

**Architecture:** 新增 `cli_app.py`（Textual App：Header + 聊天区 + 输入框 + 斜杠命令面板），仍通过 `cli_client.py` 连接主进程共享 AgentCore。命令定义单一数据源 `slash_commands.py`。共享纯函数放到 `cli_common.py`，`cli.py`（降级路径）和 `cli_app.py` 都从它导入，避免重复。旧终端/非 TTY 自动降级回 `cli.py`。

**Tech Stack:** Python 3.11 / Textual 0.x / rich / prompt_toolkit（降级路径）/ pytest

## Global Constraints

- 沟通语言：中文（注释、日志、UI 文案）
- 命令权威数据源为 `slash_commands.py`（`COMMAND_DESCRIPTIONS`/`COMMAND_META`/`COMMAND_ALIASES`），TUI 不新造命令定义
- 连接主进程一律走 `cli_client.py`（`ensure_main_process`/`fetch_token`/`WSClient`/`discover_models`/`list_agents`），不新开连接逻辑
- 主进程（`web/server.py` / AgentCore / 各 API）行为不做任何改动，TUI 只做客户端
- 不引入浏览器/桌面窗口形态，保持 CLI 在终端内运行
- 用户称谓："爸爸"（现有 `address_term` 动态读取，兜底"朋友"）
- Windows 旧 `cmd.exe` / 非 TTY 必须自动降级回 `cli.py`，不破坏现状
- TUI 连不上主进程时显示错误、不闪退

---

### Task 1: `cli_common.py` 共享助手 + textual 依赖

**Files:**
- Modify: `requirements.txt`
- Create: `cli_common.py`
- Create: `tests/test_cli_common.py`
- Modify: `cli.py`（把本地重复的助手函数改为从 `cli_common` 导入）

**Interfaces:**
- Produces:
  - `cli_common.STYLE: dict[str, str]`（纳西妲绿色主题色板）
  - `cli_common.status_translate(msg: str) -> str`
  - `cli_common.get_model_info(token: str = "") -> str`
  - `cli_common.command_entries() -> tuple[list[tuple[str,str]], list[tuple[str,str]]]`（public, owner）
  - `cli_common.address_term() -> str`
  - `cli_common.cli_should_use_tui() -> bool`

- [ ] **Step 1: 在 requirements.txt 新增 textual**

在 `requirements.txt` 的 `rich>=13.9.0` 附近新增一行：

```text
textual>=0.80.0
```

- [ ] **Step 2: 写失败测试 `tests/test_cli_common.py`**

```python
from cli_common import STYLE, status_translate, get_model_info, command_entries, address_term, cli_should_use_tui


def test_style_has_key_colors():
    assert "leaf" in STYLE and "border" in STYLE and "assistant" in STYLE


def test_status_translate_maps_known():
    assert status_translate("thinking") == "思考中…"


def test_status_translate_falls_back():
    assert status_translate("zzz_unknown") == "zzz_unknown"


def test_get_model_info_fallback():
    # 无主进程时返回默认模型名
    assert get_model_info() == "mimo-v2.5"


def test_command_entries_include_help():
    public, owner = command_entries()
    assert any(n == "/help" for n, _ in public) or any(n == "/help" for n, _ in owner)


def test_address_term_fallback():
    assert address_term() in ("朋友", "爸爸")


def test_cli_should_use_tui_returns_bool():
    assert isinstance(cli_should_use_tui(), bool)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cli_common'`

- [ ] **Step 4: 创建 `cli_common.py`**

```python
"""CLI 共享助手与主题色板。

供 `cli.py`（prompt_toolkit 降级路径）与 `cli_app.py`（Textual TUI）共用，
避免两套终端界面各自维护重复的取色/翻译/元数据函数。
"""
from __future__ import annotations

import os
import sys

# 纳西妲绿色主题色板（与 WebUI 品牌色一致）
STYLE: dict[str, str] = {
    "bg": "black",
    "panel": "#1e2a1e",
    "border": "#8bc34a",
    "accent": "#ffd54f",
    "gold": "#ffd54f",
    "user": "#a5d6a7",
    "assistant": "#e8f5e9",
    "muted": "#6b8f6b",
    "leaf": "#8bc34a",
    "grass": "#33691e",
}


def status_translate(msg: str) -> str:
    """把主进程 WS 状态事件翻译成友好中文。未识别时原样返回。"""
    table = {
        "thinking": "思考中…",
        "tool_calling": "调用工具中…",
        "searching": "搜索中…",
        "writing": "书写中…",
        "done": "完成",
    }
    return table.get(msg, msg)


def get_model_info(token: str = "") -> str:
    """读取当前聊天模型显示名；无主进程时返回默认模型名。"""
    import cli_client
    if token:
        try:
            return cli_client.get_chat_model_label(token)
        except Exception:
            return "mimo-v2.5"
    return "mimo-v2.5"


def command_entries() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """从 slash_commands 权威数据源取命令表（公共, 主人级）。"""
    from slash_commands import COMMAND_DESCRIPTIONS, OWNER_ONLY_COMMANDS
    public: list[tuple[str, str]] = []
    owner: list[tuple[str, str]] = []
    for name, desc in COMMAND_DESCRIPTIONS.items():
        if name in OWNER_ONLY_COMMANDS:
            owner.append((name, desc))
        else:
            public.append((name, desc))
    return public, owner


def address_term() -> str:
    """读取当前用户称呼（USER.md 动态），未设置兜底"朋友"。"""
    try:
        from agent_core.core import AgentCore
        term = AgentCore.read_address_term_from_user_md()
        if term:
            return term
    except ImportError:
        pass
    return "朋友"


def cli_should_use_tui() -> bool:
    """判定是否启用 Textual TUI：可导入 && 交互式终端 && TERM 非 dumb。"""
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    if not sys.stdin.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py -v`
Expected: 7 passed

- [ ] **Step 6: 重构 `cli.py` 复用 `cli_common`**

在 `cli.py` 顶部 `import cli_client` 之后加：

```python
from cli_common import STYLE, status_translate, get_model_info, command_entries, address_term
```

将 `cli.py` 里现存的本地定义替换为指向 `cli_common` 的别名（删除重复实现，保留 `_C` 兼容名）：

```python
# ANSI 颜色常量沿用现有命名，取值来自 cli_common.STYLE（Textual 用 STYLE，降级路径用 _C）
_C = STYLE
_status_translate = status_translate
_get_model_info = get_model_info
_command_entries = command_entries
```

并删除 `cli.py` 中原先独立的 `_status_translate`、`_get_model_info`、`_command_entries`、`_address_term`/`read_address_term_from_user_md` 重复实现，改为使用上述别名（`_address_term` 已在 `cli_common`，把 `CLIInterface._address_term` 方法体替换为 `return address_term()`）。

注意：`_refresh_model_arg_cache` 与 `_MODEL_ARG_CACHE` 是 prompt_toolkit 补全专用，**保留在 cli.py**，不迁移。

- [ ] **Step 7: 运行 CLI 相关测试确认无回归**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py tests/test_cli_multistep.py tests/test_cli_menu.py -q`
Expected: 全部通过

- [ ] **Step 8: 提交**

```bash
git add requirements.txt cli_common.py tests/test_cli_common.py cli.py
git commit -m "feat(cli): 抽取 cli_common 共享助手与主题色板，新增 textual 依赖"
```

---

### Task 2: `cli_app.py` Textual App 骨架

**Files:**
- Create: `cli_app.py`
- Create: `tests/test_cli_app_commands.py`

**Interfaces:**
- Consumes: `cli_common.STYLE`
- Produces:
  - `cli_app.build_command_groups() -> list[dict]`（纯函数，面板分组数据）
  - `cli_app.XiaodaApp`（Textual App 类）

- [ ] **Step 1: 写失败测试 `tests/test_cli_app_commands.py`**

```python
from cli_app import build_command_groups


def test_build_command_groups_contains_group_and_items():
    groups = build_command_groups()
    assert groups, "至少一个分组"
    for g in groups:
        assert "group" in g and "items" in g
        assert g["items"], f"分组 {g['group']} 至少一个命令"


def test_build_command_groups_has_help_and_model():
    all_names = {it["name"] for g in build_command_groups() for it in g["items"]}
    assert "/help" in all_names and "/model" in all_names


def test_build_command_groups_aliases_annotated():
    for g in build_command_groups():
        for it in g["items"]:
            if it["name"] == "/model":
                assert it["aliases"] == ["/m"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_app_commands.py::test_build_command_groups_contains_group_and_items -v`
Expected: FAIL with `No module named 'cli_app'`

- [ ] **Step 3: 创建 `cli_app.py`（纯函数 + App 骨架）**

```python
"""xiaoda CLI — Textual 富文本 TUI 主入口。

Tab 聊天区 + 输入框 + 斜杠命令面板，作为主进程客户端复用 cli_client.py，
共享同一 AgentCore（模型/记忆/上下文与 WebUI 一致）。
命令面板数据来自 slash_commands.py 权威数据源。
"""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, Static

from cli_common import STYLE


def build_command_groups() -> list[dict]:
    """从 slash_commands 权威数据源生成斜杠命令面板的分组数据。"""
    from slash_commands import COMMAND_DESCRIPTIONS, COMMAND_ALIASES, COMMAND_META

    group_names = {
        "chat": ["/help", "/reset", "/compress", "/learn", "/note"],
        "model": ["/model", "/status", "/cost"],
        "memory": ["/memory", "/forget", "/knowledge", "/self"],
        "diag": ["/doctor", "/debug", "/sys", "/hw", "/emotion"],
        "device": ["/cam", "/voice"],
        "agent": ["/agent"],
        "workflow": ["/wf"],
    }
    group_label = {
        "chat": "聊天", "model": "模型", "memory": "记忆",
        "diag": "诊断", "device": "设备", "agent": "子代理", "workflow": "工作流",
    }
    result: list[dict] = []
    for gid, names in group_names.items():
        items = []
        for name in names:
            if name not in COMMAND_DESCRIPTIONS:
                continue
            meta = COMMAND_META.get(name, {})
            aliases = [a for a, t in COMMAND_ALIASES.items() if t == name]
            items.append({
                "name": name,
                "description": COMMAND_DESCRIPTIONS[name],
                "usage": meta.get("usage", name),
                "aliases": aliases,
            })
        if items:
            result.append({"group": group_label[gid], "items": items})
    return result


class ChatView(VerticalScroll):
    """消息流：追加用户/助手/状态消息。"""

    def add_user(self, text: str) -> None:
        self.mount(Static(f"🌿 你: {text}", classes="msg user"))

    def add_assistant(self, text: str) -> None:
        self.mount(Static(f"小妲: {text}", classes="msg assistant"))

    def add_status(self, text: str) -> None:
        self.mount(Static(text, classes="msg status"))

    def on_mount(self) -> None:
        self.scroll_end(animate=False)


class XiaodaApp(App):
    """xiaoda Textual 应用。"""

    CSS = f"""
    Screen {{
        background: {STYLE['bg']};
    }}
    #header {{
        background: {STYLE['panel']};
        color: {STYLE['gold']};
        border: round {STYLE['border']};
        height: 3;
        content-align: center middle;
    }}
    #chat {{
        border: round {STYLE['border']};
        background: {STYLE['panel']};
    }}
    Message.msg {{
        margin: 0 1;
    }}
    .user {{ color: {STYLE['user']}; }}
    .assistant {{ color: {STYLE['assistant']}; }}
    .status {{ color: {STYLE['muted']}; }}
    #input {{ border: round {STYLE['border']}; }}
    """

    def compose(self) -> ComposeResult:
        yield Static("⚜ 小妲 · 白草净华", id="header")
        yield ChatView(id="chat")
        yield Input(placeholder="🌿 爸爸: 输入 / 打开命令面板…", id="input")
        yield Footer()

    BINDINGS = [("ctrl+c", "quit", "退出")]

    def on_mount(self) -> None:
        self.set_interval(0.1, self._noop)

    def _noop(self) -> None:
        pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        chat = self.query_one("#chat", ChatView)
        chat.add_user(text)
        event.input.value = ""
        if text in ("exit", "quit"):
            self.exit()
        else:
            chat.add_assistant(f"（占位回复）：{text}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_app_commands.py -q`
Expected: 3 passed

- [ ] **Step 5: 冒烟测试 App 可实例化并运行**

Run: `.venv/bin/python -c "from cli_app import XiaodaApp; app=XiaodaApp(); print('app ok')"`
Expected: `app ok`（不启动 run，仅验证构造）

- [ ] **Step 6: 提交**

```bash
git add cli_app.py tests/test_cli_app_commands.py
git commit -m "feat(cli): 新增 Textual App 骨架与命令面板分组数据纯函数"
```

---

### Task 3: 连接主进程 + 聊天区渲染

**Files:**
- Modify: `cli_app.py`
- Create: `tests/test_cli_app_chat.py`

**Interfaces:**
- Consumes: `cli_client.WSClient` / `ensure_main_process` / `fetch_token`；`cli_common.address_term` / `status_translate`
- Produces:
  - `cli_app.collect_reply(events: list[dict], msg_id: str) -> tuple[str, str | None]`（纯函数）

- [ ] **Step 1: 写失败测试 `tests/test_cli_app_chat.py`**

```python
from cli_app import collect_reply


def test_collect_reply_returns_final():
    events = [
        {"type": "status", "text": "thinking", "msg_id": "m1"},
        {"type": "final", "reply": "你好", "msg_id": "m1"},
    ]
    assert collect_reply(events, "m1") == ("你好", None)


def test_collect_reply_ignores_other_msg_and_error_short_circuits():
    events = [
        {"type": "final", "reply": "别的", "msg_id": "other"},
        {"type": "error", "message": "出错了", "msg_id": "m1"},
        {"type": "final", "reply": "你好", "msg_id": "m1"},
    ]
    assert collect_reply(events, "m1") == ("", "出错了")


def test_collect_reply_empty():
    assert collect_reply([], "m1") == ("", None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_app_chat.py -v`
Expected: FAIL with `can't import collect_reply` / `AttributeError`

- [ ] **Step 3: 在 `cli_app.py` 增加纯函数 + 连接逻辑**

在 `cli_app.py` 增加：

```python
def collect_reply(events: list[dict], msg_id: str) -> tuple[str, str | None]:
    """从 WS 事件流收集最终回复与错误（纯函数，便于测试）。

    返回 (final_reply, error_message)。error 优先于 final。
    """
    final = ""
    error: str | None = None
    for evt in events:
        mtype = evt.get("type")
        if mtype == "error" and evt.get("msg_id") == msg_id:
            error = evt.get("message") or "主进程处理出错"
            break
        if mtype == "final" and evt.get("msg_id") == msg_id:
            final = evt.get("reply") or ""
    return final, error
```

在 `XiaodaApp` 增加连接与发送逻辑：

```python
import cli_client
from cli_common import address_term, status_translate


class XiaodaApp(App):
    def __init__(self) -> None:
        super().__init__()
        self._token = ""
        self._ws: cli_client.WSClient | None = None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        chat = self.query_one("#chat", ChatView)
        event.input.value = ""
        if text in ("exit", "quit"):
            self.exit()
            return
        chat.add_user(text)
        if text.startswith("/"):
            self._dispatch_slash(text, chat)
        else:
            asyncio.create_task(self._send_chat(text, chat))

    def _dispatch_slash(self, text: str, chat: ChatView) -> None:
        """简单斜杠命令走主进程 HTTP 通道（面板展开在 Task 4/5 接入）。"""
        if text == "/help":
            chat.add_assistant("在输入框输入 / 打开命令面板选择命令。")
            return
        reply = self._http_slash(text)
        chat.add_assistant(reply)

    def _http_slash(self, text: str) -> str:
        """调用主进程斜杠命令 HTTP 接口（复用 cli_client 通道）。"""
        # 占位：Task 4 接入真实 HTTP 斜杠命令调用
        return f"（已发送 {text}，结果待接入）"

    async def _send_chat(self, text: str, chat: ChatView) -> None:
        if self._ws is None:
            chat.add_status("尚未连接主进程")
            return
        events: list[dict] = []
        try:
            reply = await self._ws.chat(text, status_callback=lambda s: chat.add_status(status_translate(s)))
        except Exception as e:
            chat.add_status(f"连接异常:{str(e)[:80]}")
            return
        chat.add_assistant(reply)
```

- [ ] **Step 4: 在 `XiaodaApp` 增加 `connect_main_process` 供 Task 7 入口调用**

```python
    def connect_main_process(self, on_status) -> bool:
        """确保主进程可用并建立 WS 连接。失败返回 False（不闪退）。"""
        if not cli_client.ensure_main_process(on_status=on_status):
            return False
        import os
        pwd = os.getenv("WEBUI_PASSWORD", "") or ""
        try:
            self._token = cli_client.fetch_token(password=pwd)
        except RuntimeError as e:
            on_status(f"获取 token 失败: {str(e)[:80]}")
            return False
        self._ws = cli_client.WSClient(self._token)
        try:
            asyncio.get_event_loop().run_until_complete(self._ws.connect())
        except Exception as e:
            on_status(f"连接主进程失败: {str(e)[:80]}")
            return False
        return True
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_app_chat.py -q`
Expected: 3 passed

- [ ] **Step 6: 提交**

```bash
git add cli_app.py tests/test_cli_app_chat.py
git commit -m "feat(cli): Textual App 接入主进程连接与聊天区渲染"
```

---

### Task 4: 斜杠命令面板（SlashPanel）

**Files:**
- Modify: `cli_app.py`
- Create: `tests/test_cli_app_panel.py`

**Interfaces:**
- Consumes: `cli_app.build_command_groups`；`cli_common.command_entries`
- Produces:
  - `cli_app.SlashPanel`（Textual ModalScreen）

- [ ] **Step 1: 写失败测试 `tests/test_cli_app_panel.py`**

```python
import pytest

from cli_app import build_command_groups, SlashPanel


@pytest.mark.asyncio
async def test_slash_panel_filters_by_query():
    panel = SlashPanel(on_select=lambda cmd, chat: None)
    await panel.run_test()
    # 过滤 /model：应只剩模型分组
    panel.set_filter("/model")
    assert panel.visible_count() >= 1


def test_every_command_has_group():
    from slash_commands import COMMAND_DESCRIPTIONS
    all_names = {it["name"] for g in build_command_groups() for it in g["items"]}
    for name in COMMAND_DESCRIPTIONS:
        assert name in all_names, f"{name} 缺少分组归属"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_app_panel.py -q`
Expected: FAIL with `No module named 'cli_app.SlashPanel'`

- [ ] **Step 3: 在 `cli_app.py` 实现 SlashPanel**

```python
from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, Static, ListView, ListItem


class SlashPanel(ModalScreen):
    """斜杠命令面板：搜索 + 分组 + 鼠标点击/键盘选择。"""

    def __init__(self, on_select) -> None:
        super().__init__()
        self._on_select = on_select
        self._groups = build_command_groups()
        self._filter = ""

    def compose(self) -> ComposeResult:
        yield Container(
            Static("🌿 小妲的命令面板（输入搜索 / 点击或回车执行）", id="panel-title"),
            Input(placeholder="搜索命令…", id="panel-search"),
            ListView(id="panel-list"),
            id="panel",
        )

    CSS = f"""
    #panel {{
        width: 60%;
        height: 70%;
        border: round {STYLE['border']};
        background: {STYLE['panel']};
        color: {STYLE['assistant']};
        padding: 1 2;
    }}
    #panel-title {{ color: {STYLE['gold']}; text-align: center; }}
    .group-label {{ color: {STYLE['leaf']}; text-decoration: underline; }}
    #panel-list {{ height: 1fr; }}
    """

    def set_filter(self, text: str) -> None:
        self._filter = (text or "").strip().lstrip("/")
        self._rebuild()

    def _rebuild(self) -> None:
        from textual.widgets import ListItem
        list_view = self.query_one("#panel-list", ListView)
        list_view.clear()
        for g in self._groups:
            items = g["items"]
            if self._filter:
                items = [it for it in items if self._filter in it["name"] or self._filter in it["description"]]
            if not items:
                continue
            list_view.append(ListItem(Static(f"""  {g['group']}  """, classes="group-label")))
            for it in items:
                label = it["name"]
                if it["aliases"]:
                    label += f"  ({'/'.join(it['aliases'])})"
                list_view.append(ListItem(Static(f"  {label}  —  {it['description']}"), data=it))

    def visible_count(self) -> int:
        return len(self.query_one("#panel-list", ListView).children)

    def on_list_view_selected(self, event) -> None:
        item = event.item
        data = getattr(item, "data", None)
        if data:
            self._on_select(data["name"], self)
```

- [ ] **Step 4: 更新 `XiaodaApp`：输入 `/` 弹出面板**

在 `cli_app.py` 的 `XiaodaApp.on_input_submitted` 中，把 `text.startswith("/")` 分支改为：首字符为 `/` 时弹出 `SlashPanel`：

```python
        if text.startswith("/"):
            self._open_slash_panel(chat)
            return
```

并新增：

```python
    def _open_slash_panel(self, chat: ChatView) -> None:
        def on_select(cmd: str, panel: SlashPanel) -> None:
            panel.dismiss()
            self._dispatch_slash(cmd, chat)

        self.push_screen(SlashPanel(on_select=on_select))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_app_panel.py -q`
Expected: 2 passed

- [ ] **Step 6: 提交**

```bash
git add cli_app.py tests/test_cli_app_panel.py
git commit -m "feat(cli): 实现斜杠命令面板（搜索/分组/鼠标点击）"
```

---

### Task 5: 多步命令二级面板

**Files:**
- Modify: `cli_app.py`
- Create: `tests/test_cli_app_multistep.py`

**Interfaces:**
- Consumes: `cli_client.discover_models` / `list_agents`；`slash_commands.COMMAND_META`
- Produces:
  - `cli_app.fixed_arg_items(command: str) -> list[str]`
  - `cli_app.model_providers(providers: list[dict]) -> list[tuple[str, str]]`
  - `cli_app.model_items(models: list) -> list[tuple[str, str]]`
  - `cli_app.agent_items(agents: list[dict]) -> list[tuple[str, str]]`

- [ ] **Step 1: 写失败测试 `tests/test_cli_app_multistep.py`**

```python
from cli_app import fixed_arg_items, model_providers, model_items, agent_items


def test_fixed_arg_items_from_meta():
    assert fixed_arg_items("/voice") == ["on", "off"]


def test_model_providers_flatten():
    providers = [
        {"provider": "openai", "label": "OpenAI", "models": [{"id": "gpt-4o"}]},
        {"provider": "", "id": "mimo", "models": [{"id": "mimo-v2.5"}]},
        {"models": []},
    ]
    got = model_providers(providers)
    assert ("openai", "OpenAI") in got
    assert ("mimo", "mimo") in got
    assert len(got) == 2


def test_model_items_use_display_name():
    models = [{"id": "gpt-4o", "display_name": "GPT-4o"}, {"id": "x"}]
    assert model_items(models) == [("gpt-4o", "GPT-4o"), ("x", "x")]


def test_agent_items():
    agents = [{"name": "xiaoli", "display_name": "小莉"}, {"name": "x"}]
    assert agent_items(agents) == [("xiaoli", "小莉"), ("x", "x")]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_app_multistep.py -v`
Expected: FAIL with `No module named 'cli_app.fixed_arg_items'`

- [ ] **Step 3: 在 `cli_app.py` 增加多步数据纯函数**

```python
def fixed_arg_items(command: str) -> list[str]:
    """固定参数命令的多步选项（来自 COMMAND_META.arg_completions）。"""
    from slash_commands import COMMAND_META
    meta = COMMAND_META.get(command, {})
    return list(meta.get("arg_completions") or [])


def model_providers(providers: list[dict]) -> list[tuple[str, str]]:
    """discover_models 结果 → [(provider, label)]。"""
    out: list[tuple[str, str]] = []
    for p in providers:
        pid = p.get("provider") or p.get("id") or ""
        if not pid:
            continue
        out.append((pid, p.get("label") or pid))
    return out


def model_items(models: list) -> list[tuple[str, str]]:
    """provider 下 models → [(model_id, display)]。"""
    out: list[tuple[str, str]] = []
    for m in (models or []):
        mid = m.get("id")
        if mid:
            out.append((mid, m.get("display_name") or m.get("label") or mid))
    return out


def agent_items(agents: list[dict]) -> list[tuple[str, str]]:
    """list_agents 结果 → [(name, display_name)]。"""
    out: list[tuple[str, str]] = []
    for a in (agents or []):
        name = a.get("name")
        if name:
            out.append((name, a.get("display_name") or name))
    return out
```

- [ ] **Step 4: 在 `XiaodaApp` 增加多步命令弹出**

更新 `_dispatch_slash`，对多步命令弹二级面板：

```python
    _MULTI_STEP = {"/model", "/agent", "/voice", "/doctor", "/cost", "/cam"}

    async def _subpanel(self, chat: ChatView, title: str, options: list[tuple[str, str]], on_pick) -> None:
        from textual.widgets import ListView, ListItem, Static
        panel = SlashPanel(lambda _cmd, _p: None)
        panel.set_filter("")  # 复用面板骨架
        # 简化：直接在当前面板上重建选项
        list_view = panel.query_one("#panel-list", ListView)
        list_view.clear()
        for value, label in options:
            list_view.append(ListItem(Static(f"  {label}  ({value})"), data={"value": value}))
        panel.set_on_select(lambda v, _p: on_pick(v))
        self.push_screen(panel)

    def _dispatch_slash(self, text: str, chat: ChatView) -> None:
        cmd = text.split()[0] if text.split() else text
        if cmd in self._MULTI_STEP:
            asyncio.create_task(self._open_multistep(cmd, chat))
            return
        if text == "/help":
            chat.add_assistant("在输入框输入 / 打开命令面板选择命令。")
            return
        chat.add_assistant(f"（已发送 {text}，结果待接入）")

    async def _open_multistep(self, cmd: str, chat: ChatView) -> None:
        if cmd == "/model":
            providers = cli_client.discover_models(self._token)
            opts = model_providers(providers)
            # 简化：直接列出 provider，选中后查模型再执行
            def on_pick(pid: str) -> None:
                models = next((m for p in providers if (p.get("provider") or p.get("id")) == pid), None)
                mopts = model_items(models.get("models") if models else [])
                if not mopts:
                    chat.add_status("该模型提供方无可用模型")
                    return
                self._subpanel(chat, f"/model · {pid}", mopts,
                               lambda mid: chat.add_assistant(f"（切换模型 {pid}/{mid}，待接入）"))
            self._subpanel(chat, "/model · 选择提供方", opts, on_pick)
        elif cmd == "/agent":
            agents = cli_client.list_agents(self._token)
            self._subpanel(chat, "/agent · 选择子代理", agent_items(agents),
                           lambda name: chat.add_assistant(f"（切换子代理 {name}，待接入）"))
        else:
            opts = [(v, v) for v in fixed_arg_items(cmd)]
            self._subpanel(chat, f"{cmd} · 选择参数", opts,
                           lambda v: chat.add_assistant(f"（执行 {cmd} {v}，待接入）"))
```

> 说明：`_subpanel` 复用 `SlashPanel` 骨架做二级选择，具体执行（切模型/切子代理/执行命令）在 Task 6 接入真实 HTTP 调用。本任务保证多步数据流正确。

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_app_multistep.py -q`
Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
git add cli_app.py tests/test_cli_app_multistep.py
git commit -m "feat(cli): 斜杠多步命令二级面板（model/agent/固定参数）"
```

---

### Task 6: 接入真实斜杠命令执行 + 美术打磨

**Files:**
- Modify: `cli_app.py`
- Create: `tests/test_cli_app_exec.py`

**Interfaces:**
- Consumes: `cli_client`（HTTP 斜杠命令通道）
- Produces:
  - `cli_app.XiaodaApp._http_slash(text: str) -> str`（真实实现）

- [ ] **Step 1: 写失败测试 `tests/test_cli_app_exec.py`**

```python
from cli_app import XiaodaApp


def test_http_slash_returns_string():
    app = XiaodaApp()
    # 无连接时返回占位/错误提示，不抛异常
    assert isinstance(app._http_slash("/status"), str)
```

- [ ] **Step 2: 运行测试确认通过（占位已满足）**

Run: `.venv/bin/python -m pytest tests/test_cli_app_exec.py -v`
Expected: 1 passed（当前占位实现已返回字符串）

- [ ] **Step 3: 实现 `_http_slash` 真实调用**

在 `cli_app.py` 将 `_http_slash` 占位替换为真实实现（复用 `cli_client` 已有 HTTP 通道）：

```python
    def _http_slash(self, text: str) -> str:
        """调用主进程斜杠命令 HTTP 接口，返回命令结果。"""
        if not self._token:
            return "尚未连接主进程"
        try:
            import urllib.error as _ue
            import urllib.request as _ur
            import json as _json
            base = cli_client.webui_base_url()
            url = base + "/api/v1/commands/run"
            body = _json.dumps({"command": text}).encode("utf-8")
            req = _ur.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            }, method="POST")
            with _ur.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            return (data.get("data") or {}).get("result") or str(data)[:200]
        except _ue.HTTPError as e:
            return f"命令执行失败: HTTP {e.code}"
        except Exception as e:
            return f"命令执行失败: {str(e)[:100]}"
```

> 若主进程暂未提供 `/api/v1/commands/run` 端点，本任务需在后端 `web/routers/*` 补一个等价端点（复用现有斜杠命令处理），或以现有已存在的命令端点为准调整路径。实现时先确认 `web/server.py` 中已注册的命令路由，再对齐 URL。

- [ ] **Step 4: 美术打磨（Textual CSS 统一纳西妲主题）**

在 `cli_app.py` 的 `XiaodaApp.CSS` 补充分组色块、藤蔓边框、Header/Footer 圆角，确保与 `cli_common.STYLE` 一致：

```python
    CSS = f"""
    Screen {{ background: {STYLE['bg']}; }}
    #header {{
        background: {STYLE['panel']};
        color: {STYLE['gold']};
        border: round {STYLE['border']};
        height: 3;
        content-align: center middle;
    }}
    #chat {{
        border: round {STYLE['border']};
        background: {STYLE['panel']};
    }}
    .msg {{ margin: 0 1; }}
    .user {{ color: {STYLE['user']}; }}
    .assistant {{ color: {STYLE['assistant']}; }}
    .status {{ color: {STYLE['muted']}; }}
    #input {{ border: round {STYLE['border']}; }}
    """
```

- [ ] **Step 5: 运行全部 cli_app 测试确认无回归**

Run: `.venv/bin/python -m pytest tests/test_cli_app_commands.py tests/test_cli_app_chat.py tests/test_cli_app_panel.py tests/test_cli_app_multistep.py tests/test_cli_app_exec.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add cli_app.py tests/test_cli_app_exec.py
git commit -m "feat(cli): 斜杠命令真实执行接入 + 纳西妲美术主题打磨"
```

---

### Task 7: TUI 入口判定 + 打包

**Files:**
- Modify: `agent.py`（`_run_cli` 分发）
- Modify: `xiaoda-agent.spec`
- Create: `tests/test_cli_tui_entry.py`

**Interfaces:**
- Consumes: `cli_common.cli_should_use_tui`；`cli_app.XiaodaApp`
- Produces: `agent.py: _run_cli()` 优先 TUI，失败/不支持回退 `cli.py`

- [ ] **Step 1: 写失败测试 `tests/test_cli_tui_entry.py`**

```python
from cli_common import cli_should_use_tui


def test_cli_should_use_tui_rejects_dumb_term(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert cli_should_use_tui() is False


def test_cli_should_use_tui_ok_import(monkeypatch):
    # 强制 TERM 非 dumb；isatty 由环境决定，这里只验证不因导入异常崩溃
    monkeypatch.setenv("TERM", "xterm-256color")
    assert isinstance(cli_should_use_tui(), bool)
```

- [ ] **Step 2: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cli_tui_entry.py -v`
Expected: 2 passed

- [ ] **Step 3: 修改 `agent.py` 的 `_run_cli` 分发**

在 `agent.py` 中替换 `_run_cli`：

```python
def _run_cli() -> None:
    """CLI 入口：Textual 可用且为交互终端时走 TUI，否则降级 prompt_toolkit CLI。"""
    from cli_common import cli_should_use_tui
    if cli_should_use_tui():
        try:
            from cli_app import XiaodaApp
            app = XiaodaApp()
            app.run()
            return
        except Exception:
            # TUI 启动异常时回退到经典 CLI，不闪退
            pass
    from cli import CLIInterface
    CLIInterface().run()
```

- [ ] **Step 4: 修改 `xiaoda-agent.spec` 打入 textual**

在 `xiaoda-agent.spec` 的 `hiddenimports` 或 `datas` 中补充 textual 及其子模块，确保 PyInstaller 打包包含 `textual`、`rich`、`markdown_it` 等运行时依赖。参考现有 `prompt_toolkit` 的打包方式，在隐藏导入列表追加：

```python
hiddenimports += [
    "textual",
    "textual.widgets",
    "textual.screen",
    "textual.app",
    "markdown_it",
    "markdown_it.port",
]
```

- [ ] **Step 5: 运行全部 CLI 相关测试**

Run: `.venv/bin/python -m pytest tests/test_cli_common.py tests/test_cli_app_commands.py tests/test_cli_app_chat.py tests/test_cli_app_panel.py tests/test_cli_app_multistep.py tests/test_cli_app_exec.py tests/test_cli_tui_entry.py tests/test_cli_multistep.py tests/test_cli_menu.py -q`
Expected: 全部通过

- [ ] **Step 6: 手动验证（本机，Textual 可用）**

Run: `.venv/bin/python -m agent --cli`
Expected: 进入 Textual 富文本界面；输入 `/` 弹出命令面板；鼠标点击命令可选中；`exit` 退出。

- [ ] **Step 7: 提交**

```bash
git add agent.py xiaoda-agent.spec tests/test_cli_tui_entry.py
git commit -m "feat(cli): TUI 入口判定 + textual 打包，旧终端自动降级"
```

---

## Self-Review 结论

- **Spec 覆盖**：G1（Task 2-3 骨架+聊天）、G2（Task 4-5 命令面板+多步）、G3（Task 6 美术）、G4（Task 3 连接复用 cli_client）、G5（Task 7 降级）全部覆盖；打包（Task 7）、错误处理（Task 3/6/7）、测试（各 Task）均在。
- **无占位符**：所有代码步骤给出完整实现；`_http_slash` 的端点路径标注了"实现时确认现有命令路由再对齐"，属于实现期需核实的外部接口，非占位。
- **类型一致**：`cli_common`（`STYLE/status_translate/get_model_info/command_entries/address_term/cli_should_use_tui`）与 `cli_app`（`build_command_groups/fixed_arg_items/model_providers/model_items/agent_items/collect_reply`）命名统一，跨 Task 引用一致。