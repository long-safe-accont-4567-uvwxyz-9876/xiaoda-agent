import asyncio
import os
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

_DEFAULT_PROJECT_DIR = os.path.expanduser("~/ai-agent")

PROTECTED_SERVICES = {"sshd", "systemd", "systemd-journald", "systemd-logind", "systemd-udevd", "dbus", "cron", "rsyslog", "networking", "NetworkManager", "ufw"}
AGENT_SERVICES = {"xiaoda-web", "qq-agent", "napcat", "qqbot", "nginx", "frpc", "docker"}


async def _run_cmd(args: list[str], timeout: int = 30, cwd: str | None = None) -> tuple[int, str, str]:
    """异步执行命令，返回 (returncode, stdout, stderr)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise


@register_tool(
    name="service_manage",
    description="系统服务管理工具。支持查看服务状态(status)、重启服务(restart)、列出Agent相关服务(list)、查看服务日志(logs)。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "restart", "list", "logs"], "description": "操作类型"},
            "name": {"type": "string", "description": "服务名称"},
            "lines": {"type": "integer", "description": "日志行数，默认30", "default": 30},
        },
        "required": ["action"],
    },
    permission=ToolPermission.EXECUTE,
    category="system",
    max_frequency=10,
    requires_confirmation=True,
)
async def service_manage(action: str, name: str = "", lines: int = 30) -> ToolResult:
    try:
        if action == "status":
            if not name:
                return ToolResult.fail("请指定服务名称")
            rc, stdout, stderr = await _run_cmd(["systemctl", "status", name, "--no-pager"], timeout=30)
            output = stdout or stderr
            if rc == 0:
                return ToolResult.ok(f"服务 {name} 状态:\n{output.strip()}")
            elif rc == 3:
                return ToolResult.ok(f"服务 {name} 状态:\n{output.strip()}")
            elif rc == 4:
                return ToolResult.fail(f"服务 {name} 不存在")
            return ToolResult.ok(f"服务 {name} 状态:\n{output.strip()}")

        elif action == "restart":
            if not name:
                return ToolResult.fail("请指定服务名称")
            if name in PROTECTED_SERVICES:
                return ToolResult.fail(f"服务 {name} 是受保护的核心服务，不允许重启")
            rc, stdout, stderr = await _run_cmd(["systemctl", "restart", name], timeout=30)
            if rc == 0:
                _, status_out, _ = await _run_cmd(["systemctl", "is-active", name], timeout=10)
                return ToolResult.ok(f"服务 {name} 已重启，当前状态: {status_out.strip()}")
            return ToolResult.fail(f"重启服务 {name} 失败: {stderr.strip()}")

        elif action == "list":
            results = []
            for svc in sorted(AGENT_SERVICES):
                rc, check_out, _ = await _run_cmd(["systemctl", "list-unit-files", f"{svc}.service"], timeout=10)
                if f"{svc}.service" not in check_out:
                    continue
                _, status_out, _ = await _run_cmd(["systemctl", "is-active", svc], timeout=10)
                _, enable_out, _ = await _run_cmd(["systemctl", "is-enabled", svc], timeout=10)
                active = status_out.strip()
                enabled = enable_out.strip()
                icon = "🟢" if active == "active" else "🔴" if active == "inactive" else "🟡"
                results.append(f"{icon} {svc}: 运行状态={active}, 开机启动={enabled}")
            if not results:
                return ToolResult.ok("未找到任何Agent相关服务")
            return ToolResult.ok("Agent相关服务列表:\n" + "\n".join(results))

        elif action == "logs":
            if not name:
                return ToolResult.fail("请指定服务名称")
            rc, stdout, stderr = await _run_cmd(["journalctl", "-u", name, "-n", str(lines), "--no-pager"], timeout=30)
            if rc != 0:
                return ToolResult.fail(f"获取服务 {name} 日志失败: {stderr.strip()}")
            # 空结果视为失败：可能服务名错误（如 nahida 而非 xiaoda-web）或服务未运行
            if not stdout.strip() or stdout.strip() == "-- No entries --":
                return ToolResult.fail(
                    f"服务 {name} 没有日志记录（可能服务名错误或服务未运行）。"
                    f"已知 Agent 服务见 service_manage(action='list')，"
                    f"本机 Agent 服务通常是 'xiaoda-web'。"
                )
            return ToolResult.ok(f"服务 {name} 最近 {lines} 条日志:\n{stdout.strip()}")

        else:
            return ToolResult.fail(f"不支持的操作: {action}")

    except asyncio.TimeoutError:
        return ToolResult.fail(f"操作超时: {action}")
    except Exception as e:
        return ToolResult.fail(f"服务管理错误: {str(e)}")


@register_tool(
    name="network_diag",
    description="网络诊断工具。支持查看网络接口(interfaces)、测试连通性(ping)、查看监听端口(ports)、测试DNS解析(dns)。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["interfaces", "ping", "ports", "dns"], "description": "诊断操作类型"},
            "target": {"type": "string", "description": "目标地址，默认8.8.8.8", "default": "8.8.8.8"},
            "count": {"type": "integer", "description": "ping次数，默认3", "default": 3},
        },
        "required": ["action"],
    },
    permission=ToolPermission.READ_ONLY,
    category="system",
    max_frequency=15,
)
async def network_diag(action: str, target: str = "8.8.8.8", count: int = 3) -> ToolResult:
    try:
        if action == "interfaces":
            rc, stdout, stderr = await _run_cmd(["ip", "-j", "addr"], timeout=30)
            if rc == 0 and stdout.strip():
                import json
                try:
                    interfaces = json.loads(stdout)
                    lines = []
                    for iface in interfaces:
                        name = iface.get("ifname", "未知")
                        state = iface.get("operstate", "未知")
                        addrs = []
                        for addr_info in iface.get("addr_info", []):
                            ip = addr_info.get("local", "")
                            prefix = addr_info.get("prefixlen", "")
                            family = "IPv4" if addr_info.get("family") == "inet" else "IPv6"
                            if ip:
                                addrs.append(f"{family}: {ip}/{prefix}")
                        status = "启用" if state == "UP" else "停用"
                        lines.append(f"接口 {name} [{status}]")
                        for a in addrs:
                            lines.append(f"  {a}")
                    return ToolResult.ok("网络接口信息:\n" + "\n".join(lines))
                except json.JSONDecodeError:
                    pass
            rc, stdout, stderr = await _run_cmd(["ip", "addr"], timeout=30)
            if rc == 0:
                return ToolResult.ok(f"网络接口信息:\n{stdout.strip()}")
            return ToolResult.fail("获取网络接口信息失败")

        elif action == "ping":
            rc, stdout, stderr = await _run_cmd(["ping", "-c", str(count), "-W", "3", target], timeout=30)
            if rc == 0:
                lines = stdout.strip().split("\n")
                summary = lines[-2] if len(lines) >= 2 else ""
                stats = lines[-1] if len(lines) >= 1 else ""
                return ToolResult.ok(f"Ping {target} 结果:\n{summary}\n{stats}")
            return ToolResult.fail(f"Ping {target} 失败，目标不可达")

        elif action == "ports":
            rc, stdout, stderr = await _run_cmd(["ss", "-tlnp"], timeout=30)
            if rc != 0:
                rc, stdout, stderr = await _run_cmd(["netstat", "-tlnp"], timeout=30)
            if rc == 0:
                lines = stdout.strip().split("\n")
                output_lines = [lines[0]] if lines else []
                for line in lines[1:]:
                    if "LISTEN" in line or "Local" in line:
                        output_lines.append(line)
                return ToolResult.ok("监听端口列表:\n" + "\n".join(output_lines))
            return ToolResult.fail("获取监听端口信息失败")

        elif action == "dns":
            rc, stdout, stderr = await _run_cmd(["nslookup", target], timeout=30)
            if rc == 0 and stdout.strip():
                return ToolResult.ok(f"DNS解析 {target}:\n{stdout.strip()}")
            rc, stdout, stderr = await _run_cmd(["dig", "+short", target], timeout=30)
            if rc == 0 and stdout.strip():
                return ToolResult.ok(f"DNS解析 {target}:\n{stdout.strip()}")
            return ToolResult.fail(f"DNS解析 {target} 失败")

        else:
            return ToolResult.fail(f"不支持的操作: {action}")

    except asyncio.TimeoutError:
        return ToolResult.fail(f"操作超时: {action}")
    except Exception as e:
        return ToolResult.fail(f"网络诊断错误: {str(e)}")


@register_tool(
    name="dev_assist",
    description="开发辅助工具。仅在用户明确要求开发调试相关操作时使用。支持查看Git状态(git_status)、检查Python依赖(pip_check)、查看Agent日志(logs)、查看项目结构(project_tree)。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["git_status", "pip_check", "logs", "project_tree"], "description": "操作类型"},
            "path": {"type": "string", "description": "项目路径", "default": _DEFAULT_PROJECT_DIR},
            "lines": {"type": "integer", "description": "日志行数，默认50", "default": 50},
            "service": {"type": "string", "description": "服务名称(用于日志)，默认xiaoda-web", "default": "xiaoda-web"},
        },
        "required": ["action"],
    },
    permission=ToolPermission.READ_ONLY,
    category="system",
    max_frequency=15,
)
async def dev_assist(action: str, path: str = _DEFAULT_PROJECT_DIR, lines: int = 50, service: str = "xiaoda-web") -> ToolResult:
    try:
        if action == "git_status":
            return await _dev_assist_git_status(path)

        elif action == "pip_check":
            return await _dev_assist_pip_check()

        elif action == "logs":
            return await _dev_assist_logs(path, lines, service)

        elif action == "project_tree":
            if not os.path.isdir(path):
                return ToolResult.fail(f"路径不存在: {path}")
            rc, stdout, stderr = await _run_cmd(
                ["find", path, "-maxdepth", "2",
                 "-not", "-path", "*/__pycache__/*",
                 "-not", "-path", "*/.git/*",
                 "-not", "-name", "*.pyc"],
                timeout=30,
            )
            if rc == 0:
                output_lines = stdout.strip().split("\n")[:50]
                return ToolResult.ok(f"项目结构 ({path}):\n" + "\n".join(output_lines))
            return ToolResult.fail("获取项目结构失败")

        else:
            return ToolResult.fail(f"不支持的操作: {action}")

    except asyncio.TimeoutError:
        return ToolResult.fail(f"操作超时: {action}")
    except Exception as e:
        return ToolResult.fail(f"开发辅助错误: {str(e)}")


async def _dev_assist_git_status(path: str) -> ToolResult:
    """查看 Git 仓库状态。"""
    if not os.path.isdir(os.path.join(path, ".git")):
        return ToolResult.fail(f"{path} 不是Git仓库")
    _, status_out, _ = await _run_cmd(["git", "status", "--short"], timeout=30, cwd=path)
    _, log_out, _ = await _run_cmd(["git", "log", "--oneline", "-5"], timeout=30, cwd=path)
    parts = []
    if status_out.strip():
        parts.append(f"文件变更:\n{status_out.strip()}")
    else:
        parts.append("工作区干净，无未提交的变更")
    if log_out.strip():
        parts.append(f"最近提交:\n{log_out.strip()}")
    return ToolResult.ok(f"Git仓库状态 ({path}):\n" + "\n\n".join(parts))


async def _dev_assist_pip_check() -> ToolResult:
    """检查 Python 依赖。"""
    _, check_out, _ = await _run_cmd(["pip", "check"], timeout=30)
    _, list_out, _ = await _run_cmd(["pip", "list", "--format=columns"], timeout=30)
    key_packages = ["openai", "aiosqlite", "loguru", "qq-botpy", "aiohttp", "fastapi", "uvicorn", "pydantic", "httpx"]
    parts = []
    if check_out.strip():
        parts.append(f"依赖检查:\n{check_out.strip()}")
    else:
        parts.append("依赖检查: 无冲突")
    installed = {}
    for line in list_out.strip().split("\n")[2:]:
        cols = line.split()
        if len(cols) >= 2:
            installed[cols[0].lower()] = cols[1]
    pkg_lines = []
    for pkg in key_packages:
        ver = installed.get(pkg.lower())
        if ver:
            pkg_lines.append(f"  ✅ {pkg} ({ver})")
        else:
            pkg_lines.append(f"  ❌ {pkg} (未安装)")
    parts.append("关键依赖包:\n" + "\n".join(pkg_lines))
    return ToolResult.ok("\n\n".join(parts))


async def _dev_assist_logs(path: str, lines: int, service: str) -> ToolResult:
    """查看 Agent 日志。"""
    # 优先用 config.LOG_DIR（外置盘持久化路径），其次回退到项目内 logs 目录
    try:
        from config import LOG_DIR
        log_dir = LOG_DIR
    except Exception:
        log_dir = os.path.join(path, "logs")
    if os.path.isdir(log_dir):
        # 兼容 .log 和 .json 两种日志格式（logging_config 写的是 agent_YYYY-MM-DD.json）
        log_files = sorted(
            [f for f in os.listdir(log_dir) if f.endswith(".log") or f.endswith(".json")],
            key=lambda f: os.path.getmtime(os.path.join(log_dir, f)),
            reverse=True
        )
        if not log_files:
            rc, log_out, _ = await _run_cmd(["journalctl", "-u", service, "-n", str(lines), "--no-pager"], timeout=30)
            if rc == 0 and log_out.strip() and log_out.strip() != "-- No entries --":
                return ToolResult.ok(f"服务 {service} 最近 {lines} 条日志:\n{log_out.strip()}")
            return ToolResult.fail(f"未找到日志文件，且服务 {service} 无 journalctl 记录（可能服务名错误，本机通常是 'xiaoda-web'）")
        log_path = os.path.join(log_dir, log_files[0])
        try:
            all_lines = await asyncio.to_thread(
                lambda: open(log_path, "r", encoding="utf-8", errors="replace").readlines()
            )
            recent = all_lines[-lines:]
            return ToolResult.ok(f"日志文件 {log_files[0]} (最后{len(recent)}行):\n{''.join(recent).strip()}")
        except Exception as e:
            return ToolResult.fail(f"读取日志文件失败: {str(e)}")
    else:
        rc, log_out, _ = await _run_cmd(["journalctl", "-u", service, "-n", str(lines), "--no-pager"], timeout=30)
        if rc == 0 and log_out.strip() and log_out.strip() != "-- No entries --":
            return ToolResult.ok(f"服务 {service} 最近 {lines} 条日志:\n{log_out.strip()}")
        return ToolResult.fail(f"未找到日志目录或日志（服务 {service} 可能服务名错误，本机通常是 'xiaoda-web'）")
