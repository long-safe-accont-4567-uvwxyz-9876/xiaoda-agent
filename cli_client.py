"""CLI 主进程客户端：让 CLI 与 Web UI / QQ / 微信共享同一个 AgentCore。

历史背景：CLI 原本是独立进程、自建 AgentCore()，与主进程（systemd xiaoda-agent）
完全隔离 —— 记忆、模型、上下文各一套，互不共享。用户要求 CLI 作为主进程内的
一个"频道"，复用已初始化的共享 AgentCore。

实现：本模块把 CLI 变成主进程的客户端 ——
  - 通过 HTTP 调 /api/v1/* 完成 token 认证与斜杠命令（/model /status /reset 等）
  - 通过 WebSocket /ws 发送对话并接收最终回复（复用与 WebUI 相同的通道）
主进程的 core.process() 内部会处理斜杠命令，因此 CLI 无需任何本地 AgentCore。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any

from loguru import logger

from utils.common import DEFAULT_WEBUI_PORT

# 与 WebUI 一致的主机/端口（agent.py 默认 WEBUI_PORT=8082）
_DEFAULT_HOST = "127.0.0.1"
# 兜底端口固定为代码默认 8082，不在此处读取环境变量 —— 否则 WEBUI_PORT 被设为
# 非数字（如 "abc"）时 int() 会在模块导入阶段抛 ValueError，导致整个模块崩溃。
# 环境变量统一由 _resolve_port() 运行时解析并做 isdigit() 校验。
_FALLBACK_PORT = DEFAULT_WEBUI_PORT
_SYSTEMD_SERVICE = "xiaoda-agent"
# 会话级缓存：首次解析后的端口在整个 CLI 生命周期内复用，避免每次构造 URL /
# 探测端口都重复执行 systemctl cat 子进程（确保轮询循环每秒一次也不起子进程）。
_RESOLVED_PORT: int | None = None


def _resolve_port(explicit: int | None = None) -> int:
    """确定主进程实际监听端口。

    服务可能用非默认端口启动（如 systemd 单元 ExecStart 里 --port 8080），
    因此 CLI 必须与实际端口对齐，否则连不上共享进程。解析顺序：
      1. explicit：调用方显式传入的端口（默认 None，优先）
      2. WEBUI_PORT 环境变量（isdigit 校验，非法值跳过）
      3. systemd 单元 xiaoda-agent 的 ExecStart --port 参数（兼容空格 / 等号两种写法）
      4. 兜底 8082
    首次解析结果缓存到 _RESOLVED_PORT，后续调用直接返回。
    """
    if explicit is not None:
        return int(explicit)
    global _RESOLVED_PORT
    if _RESOLVED_PORT is not None:
        return _RESOLVED_PORT
    env_port = os.getenv("WEBUI_PORT", "").strip()
    if env_port.isdigit():
        _RESOLVED_PORT = int(env_port)
        return _RESOLVED_PORT
    try:
        out = subprocess.run(
            ["systemctl", "cat", _SYSTEMD_SERVICE],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        # 兼容 "--port 8080" 与 "--port=8080" 两种 argparse 写法
        m = re.search(r"--port\s*=\s*(\d+)", out) or re.search(r"--port\s+(\d+)", out)
        if m:
            _RESOLVED_PORT = int(m.group(1))
            return _RESOLVED_PORT
    except Exception as e:
        logger.warning("cli.resolve_port_systemctl_failed error={}", str(e))
    _RESOLVED_PORT = _FALLBACK_PORT
    return _RESOLVED_PORT


def webui_base_url(host: str | None = None, port: int | None = None) -> str:
    """主进程 HTTP 基础地址（含 /api/v1 前的前缀）。"""
    h = host or _DEFAULT_HOST
    p = _resolve_port(port)
    return f"http://{h}:{p}"


def ws_url(host: str | None = None, port: int | None = None) -> str:
    """主进程 WebSocket 地址（/ws 无 /api/v1 前缀）。

    token 通过 WebSocket subprotocol 传递，不再拼进 URL 查询串。
    """
    h = host or _DEFAULT_HOST
    p = _resolve_port(port)
    return f"ws://{h}:{p}/ws"


def main_process_alive(host: str | None = None, port: int | None = None,
                       timeout: float = 1.0) -> bool:
    """探测主进程 HTTP 端口是否可达（不建立业务请求，仅 TCP 探测）。"""
    h = host or _DEFAULT_HOST
    p = _resolve_port(port)
    try:
        with socket.create_connection((h, p), timeout=timeout):
            return True
    except OSError:
        return False


def _main_process_cmd(port: int) -> list[str]:
    """构造主进程启动命令（跨平台）。

    主进程就是 agent 的 Web 模式（WebUI），与 CLI 同属一个安装：
      - 打包（PyInstaller frozen）：复用当前可执行文件，加 --web
      - 源码/开发：用当前 python 解释器运行同目录 agent.py --web
    macOS 无官方安装包但可跑源码，走同一源码分支。
    显式 --host 127.0.0.1 保证本机 CLI 一定能连上（不受 WEBUI_HOST 影响）。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--web", "--host", "127.0.0.1", "--port", str(port)]
    agent_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")
    return [sys.executable, agent_py, "--web", "--host", "127.0.0.1", "--port", str(port)]


def _launch_detached(cmd: list[str]) -> bool:
    """脱离当前终端后台启动主进程，返回是否成功拉起（子进程存活）。

    - Windows：DETACHED_PROCESS + 新进程组 + 不弹控制台
    - POSIX（Linux/macOS）：start_new_session 脱离控制终端，进程不随 CLI 退出而终止
    日志丢弃（stdout/stderr 指向 DEVNULL），避免阻塞；WebUI 日志仍可查。
    """
    try:
        if os.name == "nt":
            creationflags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            proc = subprocess.Popen(
                cmd, creationflags=creationflags,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        return proc.poll() is None
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("cli_client.launch_detached_error", exc_info=True)
        return False


def _try_systemd_start(on_status: Any = None) -> bool:
    """尝试用 systemd 托管服务拉起主进程（仅 Linux）。

    返回命令是否执行成功；是否真正就绪由后续端口轮询验证。
    systemctl/sudo 任一缺失（如 Docker、最小系统）则跳过，返回 False。
    """
    import shutil
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return False
    cmd = ["systemctl", "start", _SYSTEMD_SERVICE]
    if shutil.which("sudo"):
        cmd = ["sudo"] + cmd
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if result.returncode == 0:
            return True
        if on_status:
            stderr = getattr(result, "stderr", b"")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            on_status(f"systemd 启动失败: {str(stderr).strip()[:80]}")
        return False
    except Exception as e:
        logger.debug("cli_client.systemd_start_error", exc_info=True)
        if on_status:
            on_status(f"systemd 启动失败: {str(e)[:80]}")
        return False


def _wait_main_process_alive(port: int, on_status: Any = None,
                             timeout: float = 30.0) -> bool:
    """轮询等待主进程端口就绪（最多 timeout 秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if main_process_alive(port=port, timeout=1.0):
            if on_status:
                on_status("主进程已启动 ✓")
            return True
        time.sleep(1)
    return False


def ensure_main_process(on_status: Any = None) -> bool:
    """确保主进程（WebUI）已运行，跨平台自动拉起。

    - 探测端口：已运行则直接返回
    - Linux：优先 systemd 托管服务（崩溃自动重启/开机自启）；失效则直接后台拉起
    - Windows/macOS：直接后台拉起主进程（脱离终端，不弹控制台）
    - 拉起后轮询等待端口就绪；仍不可达返回 False（由调用方报错，不闪退）
    """
    if main_process_alive():
        return True
    if on_status:
        on_status("主进程未运行，正在自动启动...")
    port = _resolve_port()

    # Linux 优先 systemd（服务托管，自动重启/自启）
    if sys.platform.startswith("linux") and _try_systemd_start(on_status):
        if _wait_main_process_alive(port, on_status):
            return True
        if on_status:
            on_status("systemd 服务未就绪，尝试直接拉起主进程...")

    # Windows/macOS，或 systemd 不可用：后台直接拉起主进程
    if _launch_detached(_main_process_cmd(port)):
        if _wait_main_process_alive(port, on_status):
            return True
    else:
        if on_status:
            on_status("主进程拉起失败，请检查安装是否完整")

    if on_status:
        on_status("主进程启动后仍不可达，请检查服务状态或端口占用")
    return False


def fetch_token(host: str | None = None, port: int | None = None,
                password: str = "") -> str:
    """从主进程获取访问 token（本机无密码时 POST /auth/login 直接签发）。

    LoginRequest.password 为必填字段，故始终携带（可为空串）。无密码时主进程
    直接签发；若 .env 配置了 WEBUI_PASSWORD 则需正确密码，失败抛 RuntimeError。
    """
    import urllib.error as _ue
    import urllib.request as _ur
    url = webui_base_url(host, port) + "/api/v1/auth/login"
    body = json.dumps({"password": password}).encode("utf-8")
    req = _ur.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            _body = json.loads(resp.read().decode("utf-8"))
    except _ue.HTTPError as e:
        raise RuntimeError(f"认证失败: HTTP {e.code}") from e
    except (_ue.URLError, TimeoutError, OSError, ValueError) as e:
        # 端口可达但服务未就绪/超时/JSON 解析失败等：统一包成 RuntimeError，
        # 供上层一次捕获并返回 False，避免 CLI 在“不闪退”路径上直接崩溃。
        raise RuntimeError(f"连接主进程失败: {str(e)[:120]}") from e
    data = _body.get("data") or {}
    token = data.get("token") or ""
    if not token:
        raise RuntimeError("主进程未返回登录 token")
    return token


def _http_get_json(path: str, token: str, host: str | None = None,
                   port: int | None = None, timeout: float = 15.0) -> Any:
    import urllib.request as _ur
    url = webui_base_url(host, port) + path
    req = _ur.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _ur.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"响应 JSON 解析失败: {str(e)[:100]}") from e


def get_chat_model_label(token: str, host: str | None = None,
                         port: int | None = None) -> str:
    """远程读取当前聊天模型显示名（对齐 WebUI，单一数据源）。"""
    try:
        data = _http_get_json("/api/v1/models/chat-model", token, host, port)
        d = data.get("data") or {}
    except (ValueError, KeyError, ImportError):
        return "mimo-v2.5"
    except Exception:
        logger.exception(".cli_client.get_chat_model_label_unexpected")
        return "mimo-v2.5"
    provider = d.get("provider", "") or ""
    model_id = d.get("model_id", "") or ""
    if provider and provider != "mimo":
        return f"{provider}/{model_id}"
    return model_id or "mimo-v2.5"


def discover_models(token: str, host: str | None = None, port: int | None = None,
                    timeout: float = 15.0) -> list[dict]:
    """从主进程拉取已发现模型（provider 分组列表）。

    GET /api/v1/models/discover 返回 Envelope(data=[{provider,label,models:[...]}, ...])。
    失败或结构不符时返回空列表，由调用方回退为手动输入。
    """
    try:
        data = _http_get_json("/api/v1/models/discover", token, host, port, timeout)
        providers = data.get("data")
        return providers if isinstance(providers, list) else []
    except Exception:
        logger.debug("cli_client.discover_models_error", exc_info=True)
        return []


def list_agents(token: str, host: str | None = None, port: int | None = None,
                timeout: float = 15.0) -> list[dict]:
    """从主进程拉取代理列表。

    GET /api/v1/agents 返回 Envelope(data=[{name,display_name,...}, ...])。
    失败或结构不符时返回空列表。
    """
    try:
        data = _http_get_json("/api/v1/agents", token, host, port, timeout)
        agents = data.get("data")
        return agents if isinstance(agents, list) else []
    except Exception:
        logger.debug("cli_client.list_agents_error", exc_info=True)
        return []


class WSClient:
    """主进程 WebSocket 客户端：发送对话并等待最终回复。

    协议与 WebUI 前端完全一致（见 web/ws_hub.py 的 /ws 端点）：
      - 发送 {"type":"chat","text":...,"msg_id":...}
      - 接收 {"type":"final","reply":...}（最终回复）
      - 接收 {"type":"status"/"stream_text"/"tool_status"}（中间状态，可选显示）
    """

    def __init__(self, token: str, host: str | None = None,
                 port: int | None = None) -> None:
        import websockets
        self._websockets = websockets
        self._url = ws_url(host, port)
        self._host = host
        self._port = port
        self._token = token
        self._ws: Any = None
        self._session_id = f"cli_{int(time.time() * 1000)}"

    async def connect(self) -> None:
        try:
            connect_kwargs: dict[str, Any] = {}
            if self._token:
                connect_kwargs["subprotocols"] = [self._token]
            self._ws = await self._websockets.connect(
                self._url, open_timeout=10, ping_interval=None, **connect_kwargs)
        except (ImportError, OSError, RuntimeError, ValueError) as e:
            raise RuntimeError(f"连接主进程失败: {str(e)[:120]}") from e

        except Exception as e:
            logger.exception(".cli_client.connect_unexpected")
            raise RuntimeError(f"连接主进程失败: {str(e)[:120]}") from e

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=5)
            except Exception:
                logger.debug("cli_client.ws_close_error", exc_info=True)
            self._ws = None

    async def _recv_until_final(self, msg_id: str,
                                status_callback: Any = None) -> str:
        """读取事件直到收到 msg_id 对应的 final，返回 reply。

        不设超时：与旧 CLI 的 bot.process() 一致，长回复不被打断。
        """
        while True:
            raw = await self._ws.recv()
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = evt.get("type", "")
            if mtype == "ping":
                # 服务端心跳：必须在心跳超时前回 pong，否则长回复期间连接被服务端关闭
                await self._ws.send(json.dumps({"type": "pong"}))
                continue
            if mtype == "final" and evt.get("msg_id") == msg_id:
                return evt.get("reply") or ""
            if mtype == "error" and evt.get("msg_id") == msg_id:
                raise RuntimeError(evt.get("message", "主进程处理出错"))
            if status_callback and mtype in ("status", "stream_text", "tool_status"):
                text = evt.get("text") or evt.get("delta") or evt.get("label") or ""
                if text:
                    try:
                        await status_callback(text)
                    except Exception as e:
                        logger.warning("cli.status_callback_failed error={}", str(e))

    async def chat(self, text: str, status_callback: Any = None) -> str:
        """发送一条消息，返回最终回复。斜杠命令由主进程共享 AgentCore 处理。"""
        if self._ws is None:
            raise RuntimeError("未连接到主进程")
        msg_id = f"cli_{int(time.time() * 1000)}_{os.getpid()}"
        await self._ws.send(json.dumps({
            "type": "chat", "text": text, "msg_id": msg_id,
            "session_id": self._session_id,
        }))
        return await self._recv_until_final(msg_id, status_callback)