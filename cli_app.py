"""xiaoda CLI — Textual 富文本 TUI 主入口。

Tab 聊天区 + 输入框 + 斜杠命令面板，作为主进程客户端复用 cli_client.py，
共享同一 AgentCore（模型/记忆/上下文与 WebUI 一致）。
命令面板数据来自 slash_commands.py 权威数据源。
"""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

import cli_client
from cli_common import STYLE, status_translate


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