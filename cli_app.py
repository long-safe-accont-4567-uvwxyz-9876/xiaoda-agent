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


def fixed_arg_items(command: str) -> list[str]:
    """固定参数命令的多步选项（来自 COMMAND_META.arg_completions）。"""
    from slash_commands import COMMAND_META
    meta = COMMAND_META.get(command, {})
    return list(meta.get("arg_completions") or [])


def model_providers(providers: list[dict]) -> list[tuple[str, str]]:
    """discover_models 结果 → [(provider, label)]。跳过无 provider/id 的项。"""
    out: list[tuple[str, str]] = []
    for p in providers:
        pid = p.get("provider") or p.get("id") or ""
        if not pid:
            continue
        out.append((pid, p.get("label") or pid))
    return out


def model_items(models: list) -> list[tuple[str, str]]:
    """provider 下 models → [(model_id, display)]，用 display_name 或 label 或 id。"""
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


class _MultiStepPanel(ModalScreen):
    """多步命令二级选择面板：展示 (label, value) 列表，选中后回调 on_select(value)。"""

    def __init__(self, title: str, options, on_select) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        yield Container(
            Static(self._title, id="panel-title"),
            ListView(
                *[self._make_item(label, value) for value, label in self._options],
                id="multistep-list",
            ),
            id="panel",
        )

    CSS = f"""
    #panel {{
        width: 60%;
        height: 50%;
        border: round {STYLE['border']};
        background: {STYLE['panel']};
        color: {STYLE['assistant']};
        padding: 1 2;
    }}
    #panel-title {{ color: {STYLE['gold']}; text-align: center; }}
    #multistep-list {{ height: 1fr; }}
    """

    def on_list_view_selected(self, event) -> None:
        item = event.item
        data = getattr(item, "data", None)
        if data:
            self._on_select(data["value"])

    @staticmethod
    def _make_item(label: str, value: str):
        from textual.widgets import ListItem
        item = ListItem(Static(f"  {label}  ({value})"))
        item.data = {"value": value}
        return item


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

    def on_input_changed(self, event) -> None:
        self.set_filter(event.value)

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

    _MULTI_STEP = {"/model", "/agent", "/voice", "/doctor", "/cost", "/cam"}

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
        if getattr(event.input, "id", None) == "panel-search":
            return
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
        cmd = text.split()[0] if text.split() else text
        if cmd in self._MULTI_STEP:
            self.run_worker(self._open_multistep(cmd, chat), exclusive=False)
            return
        if text == "/help":
            chat.add_assistant("在输入框输入 / 打开命令面板选择命令。")
            return
        chat.add_assistant(f"（{text} 结果待接入）")

    async def _open_multistep(self, cmd: str, chat: ChatView) -> None:
        """拉取多步命令的二级数据并弹出选择面板（真实执行在 Task 6 接入）。"""
        if cmd == "/model":
            providers = await asyncio.to_thread(cli_client.discover_models, self._token)
            opts = model_providers(providers)
            self.push_screen(_MultiStepPanel(
                "/model · 选择提供方", opts,
                lambda pid: self._open_model_models(chat, providers, pid)))
        elif cmd == "/agent":
            agents = await asyncio.to_thread(cli_client.list_agents, self._token)
            self.push_screen(_MultiStepPanel(
                "/agent · 选择子代理", agent_items(agents),
                lambda name: chat.add_assistant(f"（切换子代理 {name}，待接入）")))
        else:
            opts = [(v, v) for v in fixed_arg_items(cmd)]
            self.push_screen(_MultiStepPanel(
                f"{cmd} · 选择参数", opts,
                lambda v: chat.add_assistant(f"（执行 {cmd} {v}，待接入）")))

    def _open_model_models(self, chat: ChatView, providers, pid: str) -> None:
        """/model 二级：某 provider 下选择具体模型。"""
        target = next((p for p in providers if (p.get("provider") or p.get("id")) == pid), None)
        mopts = model_items(target.get("models") if target else [])
        if not mopts:
            chat.add_status("该模型提供方无可用模型")
            return
        self.push_screen(_MultiStepPanel(
            f"/model · {pid}", mopts,
            lambda mid: chat.add_assistant(f"（切换模型 {pid}/{mid}，待接入）")))

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