"""xiaoda CLI — Textual 富文本 TUI 主入口。

Tab 聊天区 + 输入框 + 斜杠命令面板，作为主进程客户端复用 cli_client.py，
共享同一 AgentCore（模型/记忆/上下文与 WebUI 一致）。
命令面板数据来自 slash_commands.py 权威数据源。
"""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, ListView, Static

import cli_client
from cli_common import STYLE, status_translate


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
    .group-label {{ color: {STYLE['leaf']}; text-style: underline; }}
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
                item = ListItem(Static(f"  {label}  —  {it['description']}"))
                item.data = it
                list_view.append(item)

    def visible_count(self) -> int:
        return len(self.query_one("#panel-list", ListView).children)

    def on_list_view_selected(self, event) -> None:
        item = event.item
        data = getattr(item, "data", None)
        if data:
            self._on_select(data["name"], self)


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
    .msg {{
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

    def __init__(self) -> None:
        super().__init__()
        self._token = ""
        self._ws: cli_client.WSClient | None = None

    def on_mount(self) -> None:
        """应用挂载后启动后台 worker 建立主进程连接。"""
        self.run_worker(self._connect_main_process(self._report_status), exclusive=True)

    def _report_status(self, text: str) -> None:
        """把连接状态写入聊天视图（须在主循环线程调用）。"""
        try:
            chat = self.query_one("#chat", ChatView)
            chat.add_status(text)
        except Exception:
            pass

    async def _connect_main_process(self, on_status) -> None:
        """异步连接主进程（在 Textual 主循环内跑，阻塞部分 offload 到线程）。

        on_status 回调可能从 ``asyncio.to_thread`` 的 worker 线程触发，因此对
        widget 的访问要用 ``call_from_thread`` 切回主循环，保证线程安全、不卡 UI。
        """
        import cli_client

        def safe_on_status(text: str) -> None:
            self.call_from_thread(on_status, text)

        ok = await asyncio.to_thread(cli_client.ensure_main_process, safe_on_status)
        if not ok:
            on_status("主进程不可达，请检查服务状态")
            return
        import os
        pwd = os.getenv("WEBUI_PASSWORD", "") or ""
        try:
            self._token = await asyncio.to_thread(cli_client.fetch_token, pwd)
        except RuntimeError as e:
            on_status(f"获取 token 失败: {str(e)[:80]}")
            return
        self._ws = cli_client.WSClient(self._token)
        try:
            await self._ws.connect()
        except Exception as e:
            on_status(f"连接主进程失败: {str(e)[:80]}")

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
            self._open_slash_panel(chat)
        else:
            asyncio.create_task(self._send_chat(text, chat))

    def _open_slash_panel(self, chat: ChatView) -> None:
        def on_select(cmd: str, panel: SlashPanel) -> None:
            panel.dismiss()
            self._dispatch_slash(cmd, chat)

        self.push_screen(SlashPanel(on_select=on_select))

    def _dispatch_slash(self, text: str, chat: ChatView) -> None:
        """简单斜杠命令本地占位（命令面板与真实调用在 Task 4/5 接入）。"""
        if text == "/help":
            chat.add_assistant("在输入框输入 / 打开命令面板选择命令。")
            return
        chat.add_assistant(f"（{text} 结果待接入）")

    async def _send_chat(self, text: str, chat: ChatView) -> None:
        if self._ws is None:
            chat.add_status("尚未连接主进程")
            return
        try:
            reply = await self._ws.chat(
                text, status_callback=lambda s: chat.add_status(status_translate(s)))
        except Exception as e:
            chat.add_status(f"连接异常:{str(e)[:80]}")
            return
        chat.add_assistant(reply)