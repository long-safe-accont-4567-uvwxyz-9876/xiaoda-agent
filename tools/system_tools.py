import subprocess
import os
from tool_registry import register_tool, ToolPermission, ToolResult


PROTECTED_SERVICES = {"sshd", "systemd", "systemd-journald", "systemd-logind", "systemd-udevd", "dbus", "cron", "rsyslog", "networking", "NetworkManager", "ufw"}
AGENT_SERVICES = {"nahida-bot", "napcat", "qqbot", "nginx", "frpc", "docker"}


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
def service_manage(action: str, name: str = "", lines: int = 30) -> ToolResult:
    try:
        if action == "status":
            if not name:
                return ToolResult.fail("请指定服务名称")
            result = subprocess.run(
                ["systemctl", "status", name, "--no-pager"],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout or result.stderr
            if result.returncode == 0:
                return ToolResult.ok(f"服务 {name} 状态:\n{output.strip()}")
            elif result.returncode == 3:
                return ToolResult.ok(f"服务 {name} 状态:\n{output.strip()}")
            elif result.returncode == 4:
                return ToolResult.fail(f"服务 {name} 不存在")
            return ToolResult.ok(f"服务 {name} 状态:\n{output.strip()}")

        elif action == "restart":
            if not name:
                return ToolResult.fail("请指定服务名称")
            if name in PROTECTED_SERVICES:
                return ToolResult.fail(f"服务 {name} 是受保护的核心服务，不允许重启")
            result = subprocess.run(
                ["systemctl", "restart", name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                status_result = subprocess.run(
                    ["systemctl", "is-active", name],
                    capture_output=True, text=True, timeout=10
                )
                return ToolResult.ok(f"服务 {name} 已重启，当前状态: {status_result.stdout.strip()}")
            return ToolResult.fail(f"重启服务 {name} 失败: {result.stderr.strip()}")

        elif action == "list":
            results = []
            for svc in sorted(AGENT_SERVICES):
                check = subprocess.run(
                    ["systemctl", "list-unit-files", f"{svc}.service"],
                    capture_output=True, text=True, timeout=10
                )
                if f"{svc}.service" not in check.stdout:
                    continue
                status = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=10
                )
                enable = subprocess.run(
                    ["systemctl", "is-enabled", svc],
                    capture_output=True, text=True, timeout=10
                )
                active = status.stdout.strip()
                enabled = enable.stdout.strip()
                icon = "🟢" if active == "active" else "🔴" if active == "inactive" else "🟡"
                results.append(f"{icon} {svc}: 运行状态={active}, 开机启动={enabled}")
            if not results:
                return ToolResult.ok("未找到任何Agent相关服务")
            return ToolResult.ok("Agent相关服务列表:\n" + "\n".join(results))

        elif action == "logs":
            if not name:
                return ToolResult.fail("请指定服务名称")
            result = subprocess.run(
                ["journalctl", "-u", name, "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return ToolResult.fail(f"获取服务 {name} 日志失败: {result.stderr.strip()}")
            return ToolResult.ok(f"服务 {name} 最近 {lines} 条日志:\n{result.stdout.strip()}")

        else:
            return ToolResult.fail(f"不支持的操作: {action}")

    except subprocess.TimeoutExpired:
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
def network_diag(action: str, target: str = "8.8.8.8", count: int = 3) -> ToolResult:
    try:
        if action == "interfaces":
            result = subprocess.run(
                ["ip", "-j", "addr"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                import json
                try:
                    interfaces = json.loads(result.stdout)
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
            result = subprocess.run(
                ["ip", "addr"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return ToolResult.ok(f"网络接口信息:\n{result.stdout.strip()}")
            return ToolResult.fail("获取网络接口信息失败")

        elif action == "ping":
            result = subprocess.run(
                ["ping", "-c", str(count), "-W", "3", target],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                summary = lines[-2] if len(lines) >= 2 else ""
                stats = lines[-1] if len(lines) >= 1 else ""
                return ToolResult.ok(f"Ping {target} 结果:\n{summary}\n{stats}")
            return ToolResult.fail(f"Ping {target} 失败，目标不可达")

        elif action == "ports":
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["netstat", "-tlnp"],
                    capture_output=True, text=True, timeout=30
                )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                output_lines = [lines[0]] if lines else []
                for line in lines[1:]:
                    if "LISTEN" in line or "Local" in line:
                        output_lines.append(line)
                return ToolResult.ok("监听端口列表:\n" + "\n".join(output_lines))
            return ToolResult.fail("获取监听端口信息失败")

        elif action == "dns":
            result = subprocess.run(
                ["nslookup", target],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return ToolResult.ok(f"DNS解析 {target}:\n{result.stdout.strip()}")
            result = subprocess.run(
                ["dig", "+short", target],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return ToolResult.ok(f"DNS解析 {target}:\n{result.stdout.strip()}")
            return ToolResult.fail(f"DNS解析 {target} 失败")

        else:
            return ToolResult.fail(f"不支持的操作: {action}")

    except subprocess.TimeoutExpired:
        return ToolResult.fail(f"操作超时: {action}")
    except Exception as e:
        return ToolResult.fail(f"网络诊断错误: {str(e)}")


@register_tool(
    name="dev_assist",
    description="开发辅助工具。支持查看Git状态(git_status)、检查Python依赖(pip_check)、查看Agent日志(logs)、查看项目结构(project_tree)。",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["git_status", "pip_check", "logs", "project_tree"], "description": "操作类型"},
            "path": {"type": "string", "description": "项目路径，默认/home/orangepi/ai-agent", "default": "/home/orangepi/ai-agent"},
            "lines": {"type": "integer", "description": "日志行数，默认50", "default": 50},
            "service": {"type": "string", "description": "服务名称(用于日志)，默认nahida", "default": "nahida"},
        },
        "required": ["action"],
    },
    permission=ToolPermission.READ_ONLY,
    category="system",
    max_frequency=15,
)
def dev_assist(action: str, path: str = "/home/orangepi/ai-agent", lines: int = 50, service: str = "nahida") -> ToolResult:
    try:
        if action == "git_status":
            if not os.path.isdir(os.path.join(path, ".git")):
                return ToolResult.fail(f"{path} 不是Git仓库")
            status_result = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True, text=True, timeout=30, cwd=path
            )
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-5"],
                capture_output=True, text=True, timeout=30, cwd=path
            )
            parts = []
            if status_result.stdout.strip():
                parts.append(f"文件变更:\n{status_result.stdout.strip()}")
            else:
                parts.append("工作区干净，无未提交的变更")
            if log_result.stdout.strip():
                parts.append(f"最近提交:\n{log_result.stdout.strip()}")
            return ToolResult.ok(f"Git仓库状态 ({path}):\n" + "\n\n".join(parts))

        elif action == "pip_check":
            check_result = subprocess.run(
                ["pip", "check"],
                capture_output=True, text=True, timeout=30
            )
            key_packages = ["openai", "aiosqlite", "loguru", "qq-botpy", "aiohttp", "fastapi", "uvicorn", "pydantic", "httpx"]
            pip_list = subprocess.run(
                ["pip", "list", "--format=columns"],
                capture_output=True, text=True, timeout=30
            )
            parts = []
            if check_result.stdout.strip():
                parts.append(f"依赖检查:\n{check_result.stdout.strip()}")
            else:
                parts.append("依赖检查: 无冲突")
            installed = {}
            if pip_list.returncode == 0:
                for line in pip_list.stdout.strip().split("\n")[2:]:
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

        elif action == "logs":
            log_dir = os.path.join(path, "logs")
            if os.path.isdir(log_dir):
                log_files = sorted(
                    [f for f in os.listdir(log_dir) if f.endswith(".log")],
                    key=lambda f: os.path.getmtime(os.path.join(log_dir, f)),
                    reverse=True
                )
                if not log_files:
                    log_result = subprocess.run(
                        ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                        capture_output=True, text=True, timeout=30
                    )
                    if log_result.returncode == 0 and log_result.stdout.strip():
                        return ToolResult.ok(f"服务 {service} 最近 {lines} 条日志:\n{log_result.stdout.strip()}")
                    return ToolResult.fail("未找到日志文件")
                log_path = os.path.join(log_dir, log_files[0])
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                    recent = all_lines[-lines:]
                    return ToolResult.ok(f"日志文件 {log_files[0]} (最后{len(recent)}行):\n{''.join(recent).strip()}")
                except Exception as e:
                    return ToolResult.fail(f"读取日志文件失败: {str(e)}")
            else:
                log_result = subprocess.run(
                    ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
                    capture_output=True, text=True, timeout=30
                )
                if log_result.returncode == 0 and log_result.stdout.strip():
                    return ToolResult.ok(f"服务 {service} 最近 {lines} 条日志:\n{log_result.stdout.strip()}")
                return ToolResult.fail("未找到日志目录或日志")

        elif action == "project_tree":
            if not os.path.isdir(path):
                return ToolResult.fail(f"路径不存在: {path}")
            result = subprocess.run(
                ["find", path, "-maxdepth", "2",
                 "-not", "-path", "*/__pycache__/*",
                 "-not", "-path", "*/.git/*",
                 "-not", "-name", "*.pyc"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                output_lines = result.stdout.strip().split("\n")[:50]
                return ToolResult.ok(f"项目结构 ({path}):\n" + "\n".join(output_lines))
            return ToolResult.fail("获取项目结构失败")

        else:
            return ToolResult.fail(f"不支持的操作: {action}")

    except subprocess.TimeoutExpired:
        return ToolResult.fail(f"操作超时: {action}")
    except Exception as e:
        return ToolResult.fail(f"开发辅助错误: {str(e)}")
