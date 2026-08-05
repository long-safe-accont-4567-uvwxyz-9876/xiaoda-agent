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