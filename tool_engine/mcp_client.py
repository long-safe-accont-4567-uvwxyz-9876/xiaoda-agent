"""MCP (Model Context Protocol) Client framework.

Connects to MCP servers via stdio (subprocess), discovers their tools,
and registers them into the existing tool_registry.
"""

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

import tool_engine.tool_registry as _tool_registry_mod
from .tool_registry import ToolPermission, ToolResult


def _resolve_command_path(command: str) -> str:
    """自动检测命令的完整路径，兼容 Windows 和 Linux。

    - 如果 command 是 npx/uvx/node 等短命令名，用 shutil.which() 查找完整路径
    - Windows 上 shutil.which("npx") 可能返回 C:\\Program Files\\nodejs\\npx.cmd
    - 如果找不到，检查常见安装路径（systemd 等受限 PATH 环境）
    - 如果仍找不到，返回原始 command（让子进程启动时报错）
    """
    if not command:
        return command
    # 如果已经是绝对路径且存在，直接返回
    if os.path.isabs(command) and os.path.exists(command):
        return command
    # 用 shutil.which 查找
    resolved = shutil.which(command)
    if resolved:
        return resolved
    # shutil.which 在 systemd 等环境中可能找不到 ~/.local/bin 下的命令
    for candidate in [
        Path.home() / ".local" / "bin" / command,
        Path("/usr/local/bin") / command,
        Path.home() / ".cargo" / "bin" / command,
    ]:
        if candidate.exists():
            return str(candidate)
    # Windows: 检查 npm 全局目录
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        if appdata:
            npm_path = Path(appdata) / "npm" / f"{command}.cmd"
            if npm_path.exists():
                return str(npm_path)
    return command  # 返回原始值，让子进程报错


@dataclass
class MCPTransportConfig:
    """MCP 服务器传输配置"""
    transport: str = "stdio"  # "stdio" | "sse" | "streamable-http"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    namespace: str = ""
    enabled: bool = True
    reconnect_attempts: int = 3
    reconnect_delay_seconds: float = 5.0
    tool_timeout_seconds: float = 60.0


class MCPClient:
    """MCP Client that connects to an MCP server via stdio, SSE, or HTTP."""

    def __init__(self, server_name: str, command: str | MCPTransportConfig = "",
                 args: list[str] | None = None, env: dict | None = None) -> None:
        # Backward compatibility: if command is a string, wrap in MCPTransportConfig
        if isinstance(command, MCPTransportConfig):
            self._config = command
        else:
            self._config = MCPTransportConfig(
                transport="stdio",
                command=command,
                args=args or [],
                env=env or {},
            )

        self.server_name = server_name
        # Keep legacy attributes for backward compatibility
        self.command = self._config.command
        self.args = self._config.args
        self.env = self._config.env or None

        self._process: asyncio.subprocess.Process | None = None
        self._http_client: Any = None  # httpx.AsyncClient for SSE/HTTP
        self._session_id: str | None = None  # MCP session ID (streamable-http)
        self._connected: bool = False
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._available: bool = False
        self._tool_names: set[str] = set()
        self._registered_names: set[str] = set()  # prefixed names in tool_registry

    # ── properties ──────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._available

    @property
    def tool_names(self) -> set[str]:
        return set(self._tool_names)

    # ── lifecycle ───────────────────────────────────────────────

    async def connect(self) -> bool:
        """根据传输类型连接 MCP 服务器"""
        if self._config.transport == "stdio":
            return await self._connect_stdio()
        elif self._config.transport == "sse":
            return await self._connect_sse()
        elif self._config.transport == "streamable-http":
            return await self._connect_http()
        else:
            logger.warning("mcp.unknown_transport", transport=self._config.transport)
            return False

    async def _do_handshake(self) -> None:
        """执行 MCP 初始化握手: initialize → initialized → tools/list.

        成功时注册发现的工具; 失败时抛出 RuntimeError.
        """
        # 1) initialize（uvx/npx 首次运行需下载安装包，给 60 秒超时）
        init_result = await self._request({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xiaoda-agent", "version": "1.0.0"},
            },
        }, timeout=60.0)

        if not init_result:
            raise RuntimeError(
                f"MCP server '{self.server_name}' initialize failed: no response")

        logger.info("mcp_client.initialized", server=self.server_name,
                    server_info=init_result.get("serverInfo", {}))

        # 2) initialized notification
        await self._notify({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })

        # 3) tools/list
        tools_result = await self._request({
            "jsonrpc": "2.0",
            "method": "tools/list",
        }, timeout=30.0)

        if not tools_result:
            raise RuntimeError(
                f"MCP server '{self.server_name}' tools/list failed: no response")

        tools = tools_result.get("tools", [])
        for tool_info in tools:
            self._register_mcp_tool(tool_info)

    async def _connect_stdio(self) -> bool:
        """通过 stdio 传输连接 MCP 服务器（子进程方式）"""
        try:
            proc_env = dict(os.environ)
            if self._config.env:
                proc_env.update(self._config.env)

            # 解析命令完整路径，兼容 Windows（npx.cmd / uvx.exe 等）
            resolved_command = _resolve_command_path(self._config.command)
            self._process = await asyncio.create_subprocess_exec(
                resolved_command,
                *self._config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )

            # Start reading loop
            self._read_task = asyncio.create_task(self._read_loop())

            await self._do_handshake()

            self._connected = True
            self._available = True
            logger.info("mcp_client.started", server=self.server_name,
                        tools=list(self._tool_names))
            return True

        except Exception as e:
            logger.error("mcp_client.start_failed", server=self.server_name, error=str(e))
            await self.stop()
            return False

    async def _connect_sse(self) -> bool:
        """通过 SSE 传输连接 MCP 服务器"""
        try:
            import httpx
            self._http_client = httpx.AsyncClient(
                base_url=self._config.url,
                headers=self._config.headers or {},
                timeout=30.0,
            )
            await self._do_handshake()
            self._connected = True
            self._available = True
            logger.info("mcp.sse_connected", url=self._config.url,
                        tools=list(self._tool_names))
            return True
        except Exception as e:
            logger.warning("mcp.sse_connect_failed", url=self._config.url, error=str(e))
            await self.stop()
            return False

    async def _connect_http(self) -> bool:
        """通过 Streamable HTTP 传输连接 MCP 服务器"""
        try:
            import httpx
            self._http_client = httpx.AsyncClient(
                base_url=self._config.url,
                headers=self._config.headers or {},
                timeout=30.0,
            )
            await self._do_handshake()
            self._connected = True
            self._available = True
            logger.info("mcp.http_connected", url=self._config.url,
                        tools=list(self._tool_names))
            return True
        except Exception as e:
            logger.warning("mcp.http_connect_failed", url=self._config.url, error=str(e))
            await self.stop()
            return False

    async def start(self) -> None:
        """Start the MCP server (backward compatible wrapper for connect)."""
        result = await self.connect()
        if not result:
            raise RuntimeError(f"MCP server '{self.server_name}' failed to start")

    async def stop(self) -> None:
        """Stop the MCP server gracefully."""
        self._available = False
        self._connected = False

        # Cancel pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        if self._process and self._process.returncode is None:
            try:
                self._process.stdin.close()
                await self._process.stdin.wait_closed()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._process = None

        # Close HTTP client for SSE/HTTP transports
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        # Unregister tools from tool_registry (使用公共 API)
        for name in self._registered_names:
            _tool_registry_mod.unregister_tool(name)
        self._registered_names.clear()
        self._tool_names.clear()

        logger.info("mcp_client.stopped", server=self.server_name)

    async def disconnect(self) -> None:
        """Disconnect from the MCP server (alias for stop)."""
        await self.stop()

    # ── tool call ───────────────────────────────────────────────

    async def call_tool(self, tool_name: str, arguments: dict, timeout: float = 30.0) -> ToolResult:
        """Call a tool on the MCP server."""
        if not self._available:
            return ToolResult.fail(f"MCP server '{self.server_name}' is not available")

        try:
            result = await self._request(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
                timeout=timeout,
            )

            if result is None:
                return ToolResult.fail(f"MCP tool '{tool_name}' call timed out")

            # Check for error in response
            if "error" in result:
                error_msg = result["error"].get("message", str(result["error"]))
                return ToolResult.fail(f"MCP tool '{tool_name}' error: {error_msg}")

            # Extract content from result
            content = result.get("content", [])
            if not content:
                return ToolResult.ok("")

            # Concatenate text content items
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)

            data = "\n".join(text_parts) if text_parts else str(content)
            is_error = result.get("isError", False)
            if is_error:
                return ToolResult.fail(data)

            return ToolResult.ok(data)

        except asyncio.TimeoutError:
            return ToolResult.fail(f"MCP tool '{tool_name}' call timed out")
        except Exception as e:
            logger.error("mcp_client.call_tool_error", server=self.server_name,
                         tool=tool_name, error=str(e))
            return ToolResult.fail(f"MCP tool '{tool_name}' error: {e}")

    # ── JSON-RPC helpers ────────────────────────────────────────

    async def _request(self, msg: dict, timeout: float = 30.0) -> dict | None:
        """Send a JSON-RPC request and wait for the response.

        统一入口: 优先走 HTTP/SSE 传输, 其次走 stdio 传输。
        """
        if self._http_client is not None:
            return await self._request_http(msg, timeout)

        if not self._process or self._process.returncode is not None:
            return None

        msg_id = self._next_id
        self._next_id += 1
        msg["id"] = msg_id

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut

        line = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
        except Exception as e:
            self._pending.pop(msg_id, None)
            fut.set_result(None)
            logger.error("mcp_client.write_error", server=self.server_name, error=str(e))
            return None

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            return None

    async def _request_http(self, msg: dict, timeout: float = 30.0) -> dict | None:
        """通过 HTTP/SSE 传输发送 JSON-RPC 请求并等待响应。"""
        msg_id = self._next_id
        self._next_id += 1
        msg["id"] = msg_id

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        try:
            async with self._http_client.stream(
                "POST",
                self._config.url,
                json=msg,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    logger.error("mcp_client.http_error_status",
                                 server=self.server_name,
                                 status=resp.status_code,
                                 body=body.decode(errors="replace")[:500])
                    return None

                # 捕获服务器返回的 session id
                session_id = resp.headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id

                content_type = resp.headers.get("content-type", "")

                if "text/event-stream" in content_type:
                    # SSE 事件流: 解析事件找到匹配 id 的结果
                    return await self._parse_sse_stream(resp, msg_id)
                else:
                    # 普通 JSON 响应
                    body = await resp.aread()
                    try:
                        data = json.loads(body)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.error("mcp_client.http_json_parse_error",
                                     server=self.server_name, error=str(e))
                        return None
                    if "error" in data:
                        return {"error": data["error"]}
                    return data.get("result")
        except Exception as e:
            logger.error("mcp_client.http_request_error",
                         server=self.server_name, error=str(e))
            return None

    async def _parse_sse_stream(self, resp: Any, msg_id: int) -> dict | None:
        """解析 SSE 事件流, 返回与 msg_id 匹配的 result 字段。"""
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "":
                # 事件边界 — 尝试解析已累积的 data
                if data_lines:
                    result = self._match_sse_data(data_lines, msg_id)
                    if result is not None:
                        return result
                    data_lines = []
        # 处理流结束时剩余的 data
        if data_lines:
            result = self._match_sse_data(data_lines, msg_id)
            if result is not None:
                return result
        return None

    @staticmethod
    def _match_sse_data(data_lines: list[str], msg_id: int) -> dict | None:
        """解析单个 SSE 事件的 data 行, 若 id 匹配则返回 result。"""
        try:
            data = json.loads("\n".join(data_lines))
        except (json.JSONDecodeError, ValueError):
            return None
        if data.get("id") == msg_id:
            if "error" in data:
                return {"error": data["error"]}
            return data.get("result")
        return None

    async def _notify(self, msg: dict) -> None:
        """Send a JSON-RPC notification (no id, no response expected).

        统一入口: 优先走 HTTP/SSE 传输, 其次走 stdio 传输。
        """
        if self._http_client is not None:
            await self._notify_http(msg)
            return

        if not self._process or self._process.returncode is not None:
            return

        line = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
        except Exception as e:
            logger.error("mcp_client.notify_error", server=self.server_name, error=str(e))

    async def _notify_http(self, msg: dict) -> None:
        """通过 HTTP/SSE 传输发送 JSON-RPC 通知 (无响应)。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = await self._http_client.post(
                self._config.url,
                json=msg,
                headers=headers,
                timeout=10.0,
            )
            # 通知接受 202 Accepted 或 200 OK
            if resp.status_code not in (200, 202):
                logger.warning("mcp_client.http_notify_status",
                               server=self.server_name, status=resp.status_code)
        except Exception as e:
            logger.error("mcp_client.http_notify_error",
                         server=self.server_name, error=str(e))

    async def _read_loop(self) -> None:
        """Continuously read lines from stdout and resolve pending futures."""
        try:
            while self._process and self._process.returncode is None:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    # EOF — server closed stdout
                    break

                line = line_bytes.decode().strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("mcp_client.invalid_json", server=self.server_name, line=line[:200])
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        # Response contains "result" on success, or "error" on failure
                        if "result" in msg:
                            fut.set_result(msg["result"])
                        elif "error" in msg:
                            fut.set_result({"error": msg["error"]})
                        else:
                            fut.set_result(msg)
                # Notifications from server (no id) are logged but not handled
                elif "method" in msg and "id" not in msg:
                    logger.debug("mcp_client.server_notification",
                                 server=self.server_name, method=msg.get("method"))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("mcp_client.read_loop_error", server=self.server_name, error=str(e))
        finally:
            self._available = False
            # Resolve any remaining pending futures with None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result(None)
            self._pending.clear()

    # ── tool registration ───────────────────────────────────────

    def _register_mcp_tool(self, tool_info: dict) -> None:
        """Register a discovered MCP tool into tool_registry."""
        original_name = tool_info.get("name", "")
        if not original_name:
            return

        prefixed_name = f"mcp_{self.server_name}_{original_name}"
        description = tool_info.get("description", f"MCP tool: {original_name}")
        input_schema = tool_info.get("inputSchema", {"type": "object", "properties": {}})

        self._tool_names.add(original_name)
        self._registered_names.add(prefixed_name)

        # Capture for closure
        server_name = self.server_name
        tool_name = original_name

        async def _mcp_tool_wrapper(**kwargs: Any) -> ToolResult:
            # We need a reference to the client; use the captured variable
            # which refers to self at registration time
            client = _mcp_client_ref
            if client is None or not client.available:
                return ToolResult.fail(f"MCP server '{server_name}' is not available")
            return await client.call_tool(tool_name, kwargs)

        # Store self reference for the wrapper
        _mcp_client_ref = self

        # Register via the decorator pattern: register_tool returns a decorator
        _tool_registry_mod.register_tool(
            name=prefixed_name,
            description=description,
            schema=input_schema,
            permission=ToolPermission.EXECUTE,
            category="mcp",
            max_frequency=10,
            requires_confirmation=False,
        )(_mcp_tool_wrapper)

        logger.debug("mcp_client.tool_registered", server=self.server_name,
                     original=original_name, prefixed=prefixed_name)


class MCPManager:
    """Manages multiple MCP clients."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._sdk_servers: dict[str, SdkMcpServer] = {}
        self._health_task: asyncio.Task | None = None
        self._tool_enabled_map: dict[str, dict[str, bool]] = {}  # {server_name: {tool_name: enabled}}
        self._allowed_stdio_commands: list[str] = []
        self._allowed_url_prefixes: list[str] = []

    async def start_all(self, configs: dict[str, dict]) -> None:
        """Start all configured MCP servers.

        configs format (backward compatible):
        {
            "git": {
                "command": "/path/to/uvx",
                "args": ["mcp-server-git", "--repository", "/path"],
                "env": {"UV_INDEX_URL": "..."}
            },
            "remote": {
                "transport": "sse",
                "url": "http://localhost:8080/sse",
                "headers": {"Authorization": "Bearer xxx"}
            },
            ...
        }
        """
        for server_name, cfg in configs.items():
            transport = cfg.get("transport", "stdio")

            if transport == "stdio":
                command = cfg.get("command", "")
                if not command:
                    logger.warning("mcp_manager.skip_no_command", server=server_name)
                    continue
                config = MCPTransportConfig(
                    transport="stdio",
                    command=command,
                    args=cfg.get("args", []),
                    env=cfg.get("env", {}),
                )
            elif transport in ("sse", "streamable-http"):
                url = cfg.get("url", "")
                if not url:
                    logger.warning("mcp_manager.skip_no_url", server=server_name)
                    continue
                config = MCPTransportConfig(
                    transport=transport,
                    url=url,
                    headers=cfg.get("headers", {}),
                    namespace=cfg.get("namespace", ""),
                    reconnect_attempts=cfg.get("reconnect_attempts", 3),
                    reconnect_delay_seconds=cfg.get("reconnect_delay_seconds", 5.0),
                    tool_timeout_seconds=cfg.get("tool_timeout_seconds", 60.0),
                )
            else:
                logger.warning("mcp_manager.skip_unknown_transport",
                               server=server_name, transport=transport)
                continue

            if not cfg.get("enabled", True):
                logger.info("mcp_manager.skip_disabled", server=server_name)
                continue

            client = MCPClient(server_name, config)
            self._clients[server_name] = client

            try:
                await client.start()
                logger.info("mcp_manager.server_started", server=server_name)
            except Exception as e:
                logger.error("mcp_manager.server_start_failed",
                             server=server_name, error=str(e))

    async def stop_all(self) -> None:
        """Stop all MCP servers."""
        # Stop health monitor
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

        for server_name, client in self._clients.items():
            try:
                await client.stop()
            except Exception as e:
                logger.error("mcp_manager.server_stop_failed",
                             server=server_name, error=str(e))
        self._clients.clear()

        # 清理 SDK MCP 服务器
        for name in list(self._sdk_servers.keys()):
            # 注销工具
            server = self._sdk_servers.pop(name)
            for tool in server.tools.values():
                full_name = f"sdk_{name}_{tool.name}"
                from .tool_registry import unregister_tool
                unregister_tool(full_name)

    # ── health monitoring ────────────────────────────────────────

    async def start_health_monitor(self, interval_seconds: int = 60) -> None:
        """启动健康监控"""
        self._health_task = asyncio.create_task(self._health_loop(interval_seconds))

    async def _health_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            for name, client in list(self._clients.items()):
                try:
                    if client._http_client:
                        resp = await client._http_client.get("/health", timeout=5.0)
                        healthy = resp.status_code < 500
                    elif client._process:
                        healthy = client._process.returncode is None
                    else:
                        healthy = client._connected

                    if not healthy:
                        logger.warning("mcp.health_check_failed", server=name)
                        await self._reconnect_server(name)
                except Exception as e:
                    logger.warning("mcp.health_check_error", server=name, error=str(e))
                    await self._reconnect_server(name)

    async def _reconnect_server(self, name: str) -> None:
        """指数退避重连"""
        client = self._clients.get(name)
        if not client:
            return
        config = client._config
        for attempt in range(config.reconnect_attempts):
            delay = config.reconnect_delay_seconds * (2 ** attempt)
            logger.info("mcp.reconnect_attempt", server=name, attempt=attempt + 1, delay=delay)
            await asyncio.sleep(delay)
            try:
                await client.disconnect()
                if await client.connect():
                    logger.info("mcp.reconnected", server=name)
                    return
            except Exception as e:
                logger.warning("mcp.reconnect_failed", server=name, attempt=attempt + 1, error=str(e))
        logger.error("mcp.reconnect_exhausted", server=name)

    # ── tool-level permissions ───────────────────────────────────

    def set_tool_enabled(self, server_name: str, tool_name: str, enabled: bool) -> None:
        """设置单个工具的启用/禁用状态"""
        if server_name not in self._tool_enabled_map:
            self._tool_enabled_map[server_name] = {}
        self._tool_enabled_map[server_name][tool_name] = enabled

    def is_tool_enabled(self, server_name: str, tool_name: str) -> bool:
        """检查工具是否启用（默认启用）"""
        server_map = self._tool_enabled_map.get(server_name, {})
        return server_map.get(tool_name, True)

    # ── dynamic server security ──────────────────────────────────

    def set_security_policy(self, allowed_stdio_commands: list[str] | None = None,
                           allowed_url_prefixes: list[str] | None = None) -> None:
        """设置动态服务器的安全策略"""
        self._allowed_stdio_commands = allowed_stdio_commands or []
        self._allowed_url_prefixes = allowed_url_prefixes or []

    def validate_dynamic_server(self, config: MCPTransportConfig) -> str | None:
        """验证动态添加的服务器配置，返回错误原因或 None"""
        if config.transport == "stdio":
            if self._allowed_stdio_commands and config.command not in self._allowed_stdio_commands:
                return f"Command '{config.command}' not in allowed list"
        elif config.transport in ("sse", "streamable-http"):
            if self._allowed_url_prefixes and not any(
                config.url.startswith(prefix) for prefix in self._allowed_url_prefixes
            ):
                return f"URL '{config.url}' does not match any allowed prefix"
        return None

    async def add_server(self, server_name: str, config: MCPTransportConfig) -> bool:
        """动态添加 MCP 服务器"""
        # Security check
        error = self.validate_dynamic_server(config)
        if error:
            logger.warning("mcp.add_server_rejected", server=server_name, reason=error)
            return False

        if not config.enabled:
            logger.info("mcp.add_server_disabled", server=server_name)
            return False

        client = MCPClient(server_name, config)
        self._clients[server_name] = client

        try:
            if await client.connect():
                logger.info("mcp_manager.server_added", server=server_name)
                return True
            else:
                logger.error("mcp_manager.server_add_failed", server=server_name)
                return False
        except Exception as e:
            logger.error("mcp_manager.server_add_failed",
                         server=server_name, error=str(e))
            return False

    def get_tools_for_agent(self, mcp_servers: list[str]) -> list[dict]:
        """Get OpenAI-format tool schemas for the specified MCP servers (filtered by tool permissions)."""
        result = []
        for server_name in mcp_servers:
            client = self._clients.get(server_name)
            if not client or not client.available:
                continue
            for prefixed_name in client._registered_names:
                tool = _tool_registry_mod.get_tool(prefixed_name)
                if tool and tool.get("max_frequency", 0) > 0:
                    # Check tool-level permission
                    original_name = prefixed_name.removeprefix(f"mcp_{server_name}_")
                    if not self.is_tool_enabled(server_name, original_name):
                        continue
                    result.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["schema"],
                        },
                    })

        # 添加 SDK MCP 工具
        for server_name, server in self._sdk_servers.items():
            if not mcp_servers or server_name in mcp_servers:
                sdk_tools = server.list_tools()
                result.extend(sdk_tools)

        return result

    def register_sdk_server(self, server: "SdkMcpServer") -> None:
        """注册进程内 SDK MCP 服务器"""
        self._sdk_servers[server.name] = server
        # 注册工具到 tool_registry
        from .tool_registry import register_tool_direct
        for tool in server.tools.values():
            full_name = f"sdk_{server.name}_{tool.name}"
            register_tool_direct(
                name=full_name,
                description=tool.description,
                func=self._make_sdk_tool_wrapper(server.name, tool.name),
                parameters=tool.input_schema,
                permission="read_only",
                category="sdk_mcp",
            )
        logger.info(f"mcp_manager.sdk_server_registered", name=server.name,
                    tools=len(server.tools))

    def _make_sdk_tool_wrapper(self, server_name: str, tool_name: str) -> Any:
        """创建 SDK MCP 工具的调用包装器"""
        async def wrapper(**kwargs: Any) -> Any:
            server = self._sdk_servers.get(server_name)
            if not server:
                from .tool_registry import ToolResult
                return ToolResult.fail(f"SDK MCP 服务器 '{server_name}' 未注册")
            result = await server.call_tool(tool_name, kwargs)
            from .tool_registry import ToolResult
            if "error" in result:
                return ToolResult.fail(result["error"])
            # 提取文本内容
            content = result.get("content", [])
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            return ToolResult.ok("\n".join(texts) if texts else str(result))
        return wrapper

    def get_client(self, server_name: str) -> MCPClient | None:
        """Get a specific MCP client."""
        return self._clients.get(server_name)


# ── SDK MCP Server（进程内 MCP）──────────────────────────

from typing import Callable, Awaitable


@dataclass
class SdkMcpTool:
    """SDK MCP 工具定义 — 进程内工具，零 IPC 开销"""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict], Awaitable[dict[str, Any]]]
    annotations: dict[str, Any] | None = None


def sdk_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
    annotations: dict[str, Any] | None = None,
) -> Callable:
    """装饰器：注册进程内 MCP 工具

    示例:
        @sdk_tool("memory_search", "搜索记忆", {"query": str, "top_k": int})
        async def memory_search(args):
            return {"content": [{"type": "text", "text": "搜索结果..."}]}
    """
    def decorator(handler: Callable[[dict], Awaitable[dict[str, Any]]]) -> SdkMcpTool:
        schema = input_schema or {}
        # 如果 schema 不是标准 JSON Schema，自动转换
        if not isinstance(schema, dict) or "type" not in schema:
            properties = {}
            for param_name, param_type in schema.items():
                if param_type is str:
                    properties[param_name] = {"type": "string"}
                elif param_type is int:
                    properties[param_name] = {"type": "integer"}
                elif param_type is float:
                    properties[param_name] = {"type": "number"}
                elif param_type is bool:
                    properties[param_name] = {"type": "boolean"}
                else:
                    properties[param_name] = {"type": "string"}
            schema = {
                "type": "object",
                "properties": properties,
                "required": list(properties.keys()),
            }
        return SdkMcpTool(
            name=name,
            description=description,
            input_schema=schema,
            handler=handler,
            annotations=annotations,
        )
    return decorator


class SdkMcpServer:
    """进程内 MCP 服务器 — 无需子进程，直接调用 Python 函数"""

    def __init__(self, name: str, version: str = "1.0.0",
                 tools: list[SdkMcpTool] | None = None) -> None:
        self.name = name
        self.version = version
        self.tools: dict[str, SdkMcpTool] = {}
        if tools:
            for tool in tools:
                self.tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        """列出所有工具（OpenAI function calling 格式）"""
        result = []
        for tool in self.tools.values():
            result.append({
                "type": "function",
                "function": {
                    "name": f"sdk_{self.name}_{tool.name}",
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return result

    async def call_tool(self, tool_name: str, arguments: dict) -> dict[str, Any]:
        """调用工具"""
        tool = self.tools.get(tool_name)
        if not tool:
            return {"error": f"Tool '{tool_name}' not found"}

        try:
            result = await tool.handler(arguments)
            return result
        except Exception as e:
            logger.error(f"sdk_mcp_server.call_tool.error", tool=tool_name, error=str(e))
            return {"error": str(e)}

    def register_tool(self, tool: SdkMcpTool) -> None:
        """注册工具"""
        self.tools[tool.name] = tool
        logger.debug(f"sdk_mcp_server.register_tool", name=tool.name, server=self.name)


def create_sdk_mcp_server(name: str, version: str = "1.0.0",
                           tools: list[SdkMcpTool] | None = None) -> SdkMcpServer:
    """创建进程内 MCP 服务器

    与外部 MCP 服务器（stdio 子进程）不同，SDK MCP 服务器
    在同一 Python 进程内运行，提供：
    - 更好的性能（无 IPC 开销）
    - 更简单的部署（单进程）
    - 更容易调试（同一进程）
    - 直接访问应用状态

    Args:
        name: 服务器唯一标识
        version: 版本号
        tools: SdkMcpTool 列表

    Returns:
        SdkMcpServer 实例
    """
    return SdkMcpServer(name=name, version=version, tools=tools)
