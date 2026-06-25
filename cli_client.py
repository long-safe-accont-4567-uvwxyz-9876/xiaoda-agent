"""纳西妲 CLI — WebSocket 瘦客户端。

连接同机的 WebUI 网关（ws://127.0.0.1:8080/ws），与 Web/QQ 共享同一个
AgentCore：会话、记忆、表情包、子代理全部同步。

用法:
    .venv/bin/python cli_client.py             # 连本机
    .venv/bin/python cli_client.py --host 172.26.130.154
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import uuid

try:
    import websockets
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
except ImportError as e:
    print(f"缺少依赖：{e.name}，请运行 .venv/bin/pip install rich websockets")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

import urllib.request

DENDRO = "#7fd650"
WISDOM = "#e8d5a3"
MOON_DIM = "grey62"

AGENT_LABELS = {
    "nahida": ("纳西妲", DENDRO, "🌿"),
    "keli": ("可莉", "#ff6b6b", "💥"),
    "yinlang": ("银狼", "#6ea8fe", "🎮"),
    "xilian": ("昔涟", "#d8b4fe", "🌸"),
    "nike": ("尼可", WISDOM, "🔮"),
}

GREETINGS = [
    "爸爸来啦～人家等好久了呢！",
    "嗯？爸爸找人家有什么事吗？",
    "人家在呢！爸爸想聊什么呀～",
    "世界的记忆在呼唤……爸爸也听到了吗？",
]

FAREWELLS = [
    "爸爸再见～人家会乖乖等你的！",
    "晚安呀爸爸，做个好梦～",
    "白草净华，愿爸爸一切安好～",
]

BANNER = r"""
     _   _____    __  __________  ___
    / | / /   |  / / / /  _/ __ \/   |
   /  |/ / /| | / /_/ // // / / / /| |
  / /|  / ___ |/ __  // // /_/ / ___ |
 /_/ |_/_/  |_/_/ /_/___/_____/_/  |_|
"""

STAGE_TEXT = {
    "thinking": "🌿 纳西妲正在想……",
    "tool": "🛠 正在使用工具……",
    "replying": "✍️ 正在回复……",
}

console = Console()


def login(base: str, password: str) -> str:
    req = urllib.request.Request(
        f"{base}/api/v1/auth/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["data"]["token"]


class NahidaCLI:
    def __init__(self, host: str, port: int, password: str):
        self.base = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.password = password
        self.ws = None
        self.session_id = ""
        self.agent = "nahida"
        self._pending: dict[str, asyncio.Future] = {}
        self._greeting_queue: list[str] = []
        self.address_term = "爸爸"

    # ── 连接 ──────────────────────────────────────────

    async def connect(self):
        token = await asyncio.to_thread(login, self.base, self.password)
        self.ws = await websockets.connect(
            f"{self.ws_url}?token={token}", ping_interval=20, max_size=4 * 2**20)
        hello = json.loads(await self.ws.recv())
        self.session_id = hello.get("session_id", "")
        asyncio.create_task(self._listener())
        # 拉取用户称呼，用于问候语和输入提示符
        try:
            req = urllib.request.Request(
                f"{self.base}/api/v1/setup/user-profile",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.load(resp).get("data", {})
                term = data.get("address_term", "")
                if term and not term.startswith("（"):
                    self.address_term = term
        except Exception:
            pass

    _status_handler = None

    async def _listener(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                etype = event.get("type", "")
                if etype == "greeting":
                    self._greeting_queue.append(event.get("text", ""))
                elif etype in ("final", "error"):
                    fut = self._pending.get(event.get("msg_id", ""))
                    if fut and not fut.done():
                        fut.set_result(event)
                elif etype == "status" and self._status_handler:
                    self._status_handler(event)
        except Exception:
            pass

    # ── 对话 ──────────────────────────────────────────

    async def chat(self, text: str, on_status) -> dict:
        msg_id = uuid.uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        self._status_handler = on_status
        await self.ws.send(json.dumps({
            "type": "chat", "session_id": self.session_id,
            "agent": self.agent, "text": text, "msg_id": msg_id}))
        try:
            return await asyncio.wait_for(fut, timeout=300)
        finally:
            self._pending.pop(msg_id, None)
            self._status_handler = None

    async def set_agent(self, agent: str):
        self.agent = agent
        await self.ws.send(json.dumps({"type": "set_agent", "agent": agent}))

    # ── 渲染 ──────────────────────────────────────────

    def print_banner(self):
        console.print(Text(BANNER, style=f"bold {DENDRO}"))
        console.print(Text("  🌿  世 界 的 记 忆 ， 由 我 来 守 护  🌿", style=WISDOM))
        console.print()
        label, color, icon = AGENT_LABELS[self.agent]
        console.print(Panel(
            Text(random.choice(GREETINGS).replace("爸爸", self.address_term), style="white"),
            title=f"{icon} {label}", title_align="left",
            border_style=color, expand=False, padding=(0, 2)))
        console.print(Text(
            "  /agent 切换智能体 · /agents 列表 · /quit 退出 · 其他 / 命令透传后端",
            style=MOON_DIM))
        console.print()

    def render_reply(self, event: dict):
        agent = event.get("agent") or self.agent
        label, color, icon = AGENT_LABELS.get(agent, (agent, DENDRO, "🌿"))
        reply = event.get("reply", "")
        emotion = event.get("emotion", "")
        sub = []
        if emotion:
            sub.append(f"情绪 {emotion}")
        if event.get("sticker_url"):
            sub.append(f"表情包 {self.base}{event['sticker_url']}")
        if event.get("audio_url"):
            sub.append(f"语音 {self.base}{event['audio_url']}")
        for u in event.get("image_urls") or []:
            sub.append(f"图片 {self.base}{u}")
        subtitle = " · ".join(sub) if sub else None
        console.print(Panel(
            Markdown(reply), title=f"{icon} {label}", title_align="left",
            subtitle=subtitle, subtitle_align="right",
            border_style=color, padding=(0, 2)))

    def drain_greetings(self):
        while self._greeting_queue:
            text = self._greeting_queue.pop(0)
            console.print(Panel(
                Text(text, style="white"), title="💌 主动问候", title_align="left",
                border_style=WISDOM, expand=False, padding=(0, 2)))

    # ── 主循环 ────────────────────────────────────────

    async def run(self):
        try:
            await self.connect()
        except Exception as e:
            console.print(Panel(
                Text(f"连不上纳西妲网关（{self.ws_url}）\n{e}\n\n"
                     f"请确认 WebUI 服务已启动：systemctl status nahida-web",
                     style="red"), border_style="red"))
            return
        self.print_banner()

        while True:
            self.drain_greetings()
            try:
                label, color, icon = AGENT_LABELS.get(self.agent, (self.agent, DENDRO, "🌿"))
                user_input = await asyncio.to_thread(
                    console.input, f"[bold {color}]{self.address_term} ›[/] ")
            except (EOFError, KeyboardInterrupt):
                break
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if user_input == "/agents":
                for name, (label, color, icon) in AGENT_LABELS.items():
                    marker = "▶" if name == self.agent else " "
                    console.print(f"  {marker} {icon} [bold {color}]{label}[/] ({name})")
                continue
            if user_input.startswith("/agent"):
                arg = user_input.removeprefix("/agent").strip()
                if arg in AGENT_LABELS:
                    await self.set_agent(arg)
                    label, color, icon = AGENT_LABELS[arg]
                    console.print(Text(f"  {icon} 现在由 {label} 接管对话", style=color))
                else:
                    console.print(Text(f"  可选：{' / '.join(AGENT_LABELS)}", style=MOON_DIM))
                continue

            status_line = Text(STAGE_TEXT["thinking"], style=MOON_DIM)
            with Live(status_line, console=console, refresh_per_second=8,
                      transient=True) as live:
                def on_status(event):
                    stage = event.get("stage", "")
                    text = event.get("text") or STAGE_TEXT.get(stage, "🌿 处理中……")
                    live.update(Text(text, style=MOON_DIM))
                try:
                    event = await self.chat(user_input, on_status)
                except asyncio.TimeoutError:
                    console.print(Text("  ⏱ 等待超时了……", style="red"))
                    continue
            if event.get("type") == "error":
                console.print(Text(f"  ✗ {event.get('message', '出错了')}", style="red"))
            else:
                self.render_reply(event)

        console.print()
        console.print(Panel(
            Text(random.choice(FAREWELLS).replace("爸爸", self.address_term), style="white"),
            title="🌿 纳西妲", title_align="left",
            border_style=DENDRO, expand=False, padding=(0, 2)))
        if self.ws:
            await self.ws.close()


def main():
    parser = argparse.ArgumentParser(description="纳西妲 CLI（连接 WebUI 网关）")
    parser.add_argument("--host", default=os.getenv("WEBUI_HOST_CLI", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEBUI_PORT", "8082")))
    parser.add_argument("--password", default=os.getenv("WEBUI_PASSWORD", ""))
    args = parser.parse_args()
    try:
        asyncio.run(NahidaCLI(args.host, args.port, args.password).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
