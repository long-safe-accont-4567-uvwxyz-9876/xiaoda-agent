"""小妲 CLI — WebSocket 瘦客户端。

连接同机的 WebUI 网关（ws://127.0.0.1:8082/ws），与 Web/QQ 共享同一个
AgentCore：会话、记忆、表情包、子代理全部同步。

用法:
    .venv/bin/python cli_client.py             # 连本机
    .venv/bin/python cli_client.py --host 172.26.130.154
"""
from __future__ import annotations
from typing import Any

import argparse
import asyncio
import json


def _safe_int(val, default):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
import os
import random
import sys
import uuid

from loguru import logger

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
import contextlib

DENDRO = "#7fd650"
WISDOM = "#e8d5a3"
MOON_DIM = "grey62"

STAGE_TEXT = {
    "thinking": "🌿 小妲正在想……",
    "tool": "🛠 正在使用工具……",
    "replying": "✍️ 正在回复……",
}

# IP-safe: 动态从 config/agents/*.json 读取 display_name，颜色/emoji 保留默认
_AGENT_STYLE_DEFAULTS = {
    "xiaoda": (DENDRO, "🌿"), "xiaoli": ("#ff6b6b", "💥"),
    "xiaolang": ("#6ea8fe", "🎮"), "xiaolian": ("#d8b4fe", "🌸"), "xiaoke": (WISDOM, "🔮"),
}
try:
    from config import get_agent_display_name, agent_names
    from emotion.emoji_config import get_ack_message
    AGENT_LABELS = {}
    for _name in agent_names():
        _color, _emoji = _AGENT_STYLE_DEFAULTS.get(_name, (DENDRO, "🤖"))
        AGENT_LABELS[_name] = (get_agent_display_name(_name), _color, _emoji)
    # ACK 消息使用自定义配置（随心即言）
    STAGE_TEXT["thinking"] = get_ack_message("xiaoda")
except ImportError:
    AGENT_LABELS = {
        "xiaoda": ("小妲", DENDRO, "🌿"),
        "xiaoli": ("小莉", "#ff6b6b", "💥"),
        "xiaolang": ("小狼", "#6ea8fe", "🎮"),
        "xiaolian": ("小涟", "#d8b4fe", "🌸"),
        "xiaoke": ("小可", WISDOM, "🔮"),
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

console = Console()


def login(base: str, password: str, retries: int = 3) -> str:
    """登录 API，带重试保护。"""
    req = urllib.request.Request(
        f"{base}/api/v1/auth/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)["data"]["token"]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries - 1:
                import time
                time.sleep(1 * (attempt + 1))  # 递增等待
    raise ConnectionError(f"登录失败（重试{retries}次）: {last_err}")


class NahidaCLI:
    """远程 Agent 的命令行客户端，通过 HTTP/WebSocket 连接服务端。"""
    def __init__(self, host: str, port: int, password: str) -> None:
        self.base = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.password = password
        self.ws = None
        self.session_id = ""
        self.agent = "xiaoda"
        self._pending: dict[str, asyncio.Future] = {}
        self._greeting_queue: list[str] = []
        self.address_term = "爸爸"
        # 动态 agent 标签：connect 成功后从 /api/v1/agents 更新；默认回退 AGENT_LABELS
        self.agent_labels: dict[str, tuple] = dict(AGENT_LABELS)

    def _agent_display_name(self, name: str) -> str:
        """返回 agent 的 display_name，优先读动态值，回退到 AGENT_LABELS 默认。"""
        labels = self.agent_labels or AGENT_LABELS
        return labels.get(name, (name, DENDRO, "🌿"))[0]

    def _stage_text(self, stage: str) -> str:
        """返回阶段提示文案，thinking 阶段使用自定义 ACK 配置。"""
        return STAGE_TEXT.get(stage, "🌿 处理中……")

    # ── 连接 ──────────────────────────────────────────

    async def connect(self) -> None:
        token = await asyncio.to_thread(login, self.base, self.password)
        self.ws = await websockets.connect(
            f"{self.ws_url}?token={token}", ping_interval=20, max_size=4 * 2**20)
        hello = json.loads(await self.ws.recv())
        self.session_id = hello.get("session_id", "")
        _listener_task = asyncio.create_task(self._listener())
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
            logger.debug("cli_client.user_profile_fetch_failed", exc_info=True)
        # 拉取各 agent 的 display_name，更新 agent_labels（覆盖默认值）
        try:
            req = urllib.request.Request(
                f"{self.base}/api/v1/agents",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                agents = json.load(resp).get("data", []) or []
            for item in agents:
                name = item.get("name", "")
                dn = item.get("display_name", "")
                if name and dn and name in self.agent_labels:
                    _, color, icon = self.agent_labels[name]
                    self.agent_labels[name] = (dn, color, icon)
        except Exception:
            logger.debug("cli_client.agents_fetch_failed", exc_info=True)

    _status_handler = None

    async def _listener(self) -> None:
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
            logger.debug("cli_client.listener_failed", exc_info=True)

    # ── 对话 ──────────────────────────────────────────

    async def chat(self, text: str, on_status: Any) -> dict:
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

    async def set_agent(self, agent: str) -> None:
        self.agent = agent
        await self.ws.send(json.dumps({"type": "set_agent", "agent": agent}))

    # ── 渲染 ──────────────────────────────────────────

    def print_banner(self) -> None:
        console.print(Text(BANNER, style=f"bold {DENDRO}"))
        console.print(Text("  🌿  世 界 的 记 忆 ， 由 我 来 守 护  🌿", style=WISDOM))
        console.print()
        label, color, icon = self.agent_labels[self.agent]
        console.print(Panel(
            Text(random.choice(GREETINGS).replace("爸爸", self.address_term), style="white"),
            title=f"{icon} {label}", title_align="left",
            border_style=color, expand=False, padding=(0, 2)))
        console.print(Text(
            "  /agent 切换智能体 · /agents 列表 · /quit 退出 · 其他 / 命令透传后端",
            style=MOON_DIM))
        console.print()

    def render_reply(self, event: dict) -> None:
        agent = event.get("agent") or self.agent
        label, color, icon = self.agent_labels.get(agent, (agent, DENDRO, "🌿"))
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

    def drain_greetings(self) -> None:
        while self._greeting_queue:
            text = self._greeting_queue.pop(0)
            console.print(Panel(
                Text(text, style="white"), title="💌 主动问候", title_align="left",
                border_style=WISDOM, expand=False, padding=(0, 2)))

    # ── 主循环 ────────────────────────────────────────

    async def run(self) -> None:
        try:
            await self.connect()
        except Exception as e:
            console.print(Panel(
                Text(f"连不上网关（{self.ws_url}）\n{e}\n\n"
                     f"请确认 WebUI 服务已启动：systemctl status xiaoda-web",
                     style="red"), border_style="red"))
            return
        self.print_banner()

        while True:
            self.drain_greetings()
            try:
                label, color, icon = self.agent_labels.get(self.agent, (self.agent, DENDRO, "🌿"))
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
                for name, (label, color, icon) in self.agent_labels.items():
                    marker = "▶" if name == self.agent else " "
                    console.print(f"  {marker} {icon} [bold {color}]{label}[/] ({name})")
                continue
            if user_input.startswith("/agent"):
                arg = user_input.removeprefix("/agent").strip()
                if arg in self.agent_labels:
                    await self.set_agent(arg)
                    label, color, icon = self.agent_labels[arg]
                    console.print(Text(f"  {icon} 现在由 {label} 接管对话", style=color))
                else:
                    console.print(Text(f"  可选：{' / '.join(self.agent_labels)}", style=MOON_DIM))
                continue

            status_line = Text(self._stage_text("thinking"), style=MOON_DIM)
            with Live(status_line, console=console, refresh_per_second=8,
                      transient=True) as live:
                def on_status(event: Any) -> None:
                    stage = event.get("stage", "")
                    text = event.get("text") or self._stage_text(stage)
                    live.update(Text(text, style=MOON_DIM))
                try:
                    event = await self.chat(user_input, on_status)
                except TimeoutError:
                    console.print(Text("  ⏱ 等待超时了……", style="red"))
                    continue
            if event.get("type") == "error":
                console.print(Text(f"  ✗ {event.get('message', '出错了')}", style="red"))
            else:
                self.render_reply(event)

        console.print()
        console.print(Panel(
            Text(random.choice(FAREWELLS).replace("爸爸", self.address_term), style="white"),
            title=f"🌿 {self._agent_display_name(self.agent)}", title_align="left",
            border_style=DENDRO, expand=False, padding=(0, 2)))
        if self.ws:
            await self.ws.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Agent CLI（连接 WebUI 网关）")
    parser.add_argument("--host", default=os.getenv("WEBUI_HOST_CLI", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_safe_int(os.getenv("WEBUI_PORT", "8082"), 8082))
    parser.add_argument("--password", default=os.getenv("WEBUI_PASSWORD", ""))
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(NahidaCLI(args.host, args.port, args.password).run())


if __name__ == "__main__":
    main()
