from typing import Any
import asyncio
import os
import re
import shlex
import tempfile
import urllib.parse
from loguru import logger
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult


# ==================== 文件路径沙箱 ====================

# 项目根目录（tools 的上级目录）
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 允许访问的基础目录白名单
ALLOWED_BASE_DIRS = [
    _PROJECT_DIR,                                                          # 项目目录
    os.path.expanduser("~/ai-agent"),                                      # 用户主目录下的项目目录
    os.path.expanduser("~"),                                               # 用户主目录（本地编辑器需要读写桌面/文档等）
    "/tmp",
    "/var/tmp",
    tempfile.gettempdir(),                                                 # 系统临时目录（Windows: C:\Users\...\AppData\Local\Temp）
    os.path.join(_PROJECT_DIR, "tts_cache"),                               # tts_cache 目录
    os.environ.get("KIOXIA_DATA_DIR", os.path.expanduser("~/.ai-agent/data")),  # 数据目录
]

# 敏感路径黑名单（即使白名单通过也不允许访问）
SENSITIVE_PATHS = [
    "/etc/shadow",
    "/etc/passwd",
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/.gnupg"),
    "/root",
]

# 规范化白名单和黑名单（realpath）
# 白名单：仅保留实际存在的目录（不存在的目录无意义，且 realpath 可能解析到意外位置）
ALLOWED_BASE_DIRS = [os.path.realpath(d) for d in ALLOWED_BASE_DIRS if os.path.exists(d)]
# 黑名单：无条件保留（即使目录不存在也要拦截）。
# 之前用 os.path.exists 过滤导致 runner/容器等无 ~/.ssh 的环境黑名单被清空，
# 攻击者创建 ~/.ssh 符号链接指向敏感文件即可绕过。realpath 对不存在路径只规范化不解析符号链接，安全。
SENSITIVE_PATHS = [os.path.realpath(d) for d in SENSITIVE_PATHS]


def _validate_path(path: str, mode: str = "read") -> tuple[bool, str, str]:
    """验证路径是否在沙箱允许范围内。

    Args:
        path: 待验证的路径
        mode: "read" 或 "write"

    Returns:
        (is_allowed, resolved_path, reason)
    """
    # 展开用户目录并解析真实路径
    expanded = os.path.abspath(os.path.expanduser(path))
    # 对于不存在的路径，先规范化目录部分
    if os.path.exists(expanded):
        resolved = os.path.realpath(expanded)
    else:
        # 路径不存在时，规范化到最近的已存在父目录 + 剩余部分
        parent = expanded
        remainder = ""
        while not os.path.exists(parent):
            prev = parent
            parent, tail = os.path.split(parent)
            remainder = os.path.join(tail, remainder) if remainder else tail
            if parent == prev:
                break
        resolved_parent = os.path.realpath(parent)
        resolved = os.path.join(resolved_parent, remainder) if remainder else resolved_parent

    # 检查敏感路径黑名单
    for sensitive in SENSITIVE_PATHS:
        if resolved == sensitive or resolved.startswith(sensitive + os.sep):
            return False, resolved, f"路径在敏感目录中，禁止访问: {sensitive}"
    # 额外检查 .env 文件
    if os.path.basename(resolved) == ".env" or "/.env" in resolved:
        return False, resolved, "禁止访问 .env 文件"

    # 检查白名单
    for allowed in ALLOWED_BASE_DIRS:
        if resolved == allowed or resolved.startswith(allowed + os.sep):
            # TOCTOU 防护：对已存在路径再次 realpath 确认无符号链接替换
            if os.path.exists(resolved):
                re_resolved = os.path.realpath(resolved)
                if re_resolved != resolved:
                    # 符号链接解析后路径不同，需要重新验证
                    for s in SENSITIVE_PATHS:
                        if re_resolved == s or re_resolved.startswith(s + os.sep):
                            return False, re_resolved, f"符号链接目标在敏感目录中: {s}"
                    in_allowed = False
                    for a in ALLOWED_BASE_DIRS:
                        if re_resolved == a or re_resolved.startswith(a + os.sep):
                            in_allowed = True
                            break
                    if not in_allowed:
                        return False, re_resolved, f"符号链接目标不在允许的目录范围内"
                    resolved = re_resolved
            # 写入模式额外限制：只允许项目目录和 tts_cache
            if mode == "write":
                write_allowed = [_PROJECT_DIR, os.path.join(_PROJECT_DIR, "tts_cache"), "/tmp", "/var/tmp", tempfile.gettempdir(), os.path.expanduser("~")]
                write_allowed = [os.path.realpath(d) for d in write_allowed if os.path.exists(d)]
                for wa in write_allowed:
                    if resolved == wa or resolved.startswith(wa + os.sep):
                        return True, resolved, ""
                return False, resolved, "写入路径不在允许的写入目录中，仅允许项目目录、tts_cache、系统临时目录"
            return True, resolved, ""

    return False, resolved, f"路径不在允许的目录范围内: {path}"


def _open_validated(resolved: str, mode: str = "r", encoding: str | None = "utf-8"):
    """原子性打开已验证的路径，防止 TOCTOU 符号链接替换攻击。

    先打开文件获取 fd，再通过 /proc/self/fd 读取 fd 的真实路径，
    确保打开的文件与验证时路径一致。如果不一致则关闭并拒绝。
    """
    try:
        fd = os.open(resolved, os.O_RDONLY if "r" in mode else os.O_RDWR)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise e

    # 通过 fd 重新解析真实路径，确保没有被符号链接替换
    try:
        fd_real_path = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        # /proc 不可用（如某些容器环境），跳过二次验证
        pass
    else:
        # 验证 fd 指向的真实路径是否仍在允许范围内
        for sensitive in SENSITIVE_PATHS:
            if fd_real_path == sensitive or fd_real_path.startswith(sensitive + os.sep):
                os.close(fd)
                raise PermissionError(f"文件描述符指向敏感目录: {sensitive}")
        in_allowed = False
        for allowed in ALLOWED_BASE_DIRS:
            if fd_real_path == allowed or fd_real_path.startswith(allowed + os.sep):
                in_allowed = True
                break
        if not in_allowed:
            os.close(fd)
            raise PermissionError(f"文件描述符指向不允许的目录: {fd_real_path}")

    if encoding:
        return os.fdopen(fd, mode, encoding=encoding, errors="ignore")
    return os.fdopen(fd, mode)


# 安全加固：拦截危险操作（开发板模式，保留基本 shell 执行能力）
BLOCKED_COMMANDS = {
    # 递归强制删除
    'rm -rf', 'rm -fr', 'rm -r -f', 'rm -f -r',
    # 磁盘操作
    'dd', 'mkfs', 'mkfs.ext4', 'mkfs.ext3', 'mkfs.vfat', 'mkfs.ntfs',
    'fdisk', 'cfdisk', 'parted', 'format',
    # 危险权限修改
    'chmod 777', 'chmod -R 777', 'chmod 000',
    'chown', 'chgrp',
    # 系统关停
    'shutdown', 'reboot', 'poweroff', 'halt', 'init 0', 'init 6',
    # 反向 shell 工具
    'nc -e', 'ncat -e', 'socat exec',
    # 危险覆盖
    'shred', 'wipe',
}

# 危险命令模式（正则，不区分大小写）
_DANGEROUS_PATTERNS = [
    # rm -rf 任意路径（不只是根目录）
    r'rm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+|--recursive\s+--force\s+)\S+',
    # fork bomb 变体
    r':\(\)\{\s*:\|:&\s*\}',
    r'\w+\(\)\{\s*\w+\|:\&\s*\}',
    r'fork\s+bomb',
    # 管道到 shell
    r'\|\s*(ba)?sh\b',
    r'\|\s*(ba)?sh\s+-c\b',
    # 命令替换（仅拦截 bash 风格，PowerShell 的 $() 是表达式不是注入）
    r'bash\s+-c\s+.*\$\([^)]*\)',
    r'`[^`]+`',
    # 反向 shell
    r'(nc|ncat|socat)\s+.*(-e|--exec)\s+',
    r'(nc|ncat)\s+.*(-e|--sh-exec)\s+',
    # curl/wget 管道到 shell
    r'(curl|wget)\s+.*\|\s*(ba)?sh',
    # 危险重定向覆盖
    r'>\s*/dev/sd[a-z]',
    r'>\s*/dev/nand',
    r'>\s*/dev/mmcblk',
    # 内核模块操作
    r'rmmod\s+',
    r'modprobe\s+-r\s+',
    # 覆写关键系统文件
    r'>\s*/etc/passwd',
    r'>\s*/etc/shadow',
    r'>\s*/etc/sudoers',
]

# 输出中需要遮蔽的敏感信息模式
_SENSITIVE_OUTPUT_PATTERNS = [
    # /etc/shadow 中的密码哈希
    (r'(\$6\$|\$5\$|\$1\$)[a-zA-Z0-9./]{0,16}\$[a-zA-Z0-9./]{1,86}', '[HASH_REDACTED]'),
    # API Key 模式
    (r'(api[_-]?key|apikey|access[_-]?token|secret[_-]?key)\s*[=:]\s*["\']?\s*[a-zA-Z0-9_\-]{20,}', r'\1=[REDACTED]'),
    # 私钥标识
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----', '[PRIVATE_KEY_REDACTED]'),
    # 常见 token 格式
    (r'(Bearer|token)\s+[a-zA-Z0-9_\-\.]{20,}', r'\1 [REDACTED]'),
]


def _normalize_command(command: str) -> str:
    """规范化命令字符串，处理编码绕过尝试"""
    normalized = command
    # URL 解码（可能多层编码）
    for _ in range(3):
        try:
            decoded = urllib.parse.unquote(normalized)
            if decoded == normalized:
                break
            normalized = decoded
        except Exception:
            logger.debug("file_tools.url_decode_error", exc_info=True)
            break
    # hex 编码绕过：\xHH 格式
    def _replace_hex(m: Any) -> Any:
        r"""将 \xHH 十六进制转义序列还原为对应字符。"""
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    normalized = re.sub(r'\\x([0-9a-fA-F]{2})', _replace_hex, normalized)
    # octal 编码绕过：\OOO 格式
    def _replace_octal(m: Any) -> Any:
        r"""将 \OOO 八进制转义序列还原为对应字符。"""
        try:
            return chr(int(m.group(1), 8))
        except ValueError:
            return m.group(0)
    normalized = re.sub(r'\\([0-7]{3})', _replace_octal, normalized)
    # unicode 编码绕过：\uHHHH 格式
    def _replace_unicode(m: Any) -> Any:
        r"""将 \uHHHH Unicode 转义序列还原为对应字符。"""
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    normalized = re.sub(r'\\u([0-9a-fA-F]{4})', _replace_unicode, normalized)
    # 去除多余空格
    return ' '.join(normalized.split())


def _is_command_dangerous(command: str) -> str | None:
    """检查命令是否危险，返回危险原因或 None。开发板模式：拦截危险操作"""
    # 规范化：处理编码绕过
    normalized = _normalize_command(command)

    # 检查基本黑名单（对规范化后的命令检查）
    for blocked in BLOCKED_COMMANDS:
        if blocked in normalized or blocked in command:
            return f"命令包含不允许的操作: {blocked}"

    # 检查危险模式
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return f"命令包含危险模式: {pattern}"
        if re.search(pattern, command, re.IGNORECASE):
            return f"命令包含危险模式: {pattern}"

    return None


def _sanitize_output(output: str) -> str:
    """对命令输出中的敏感信息进行遮蔽"""
    sanitized = output
    for pattern, replacement in _SENSITIVE_OUTPUT_PATTERNS:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    return sanitized


@register_tool(
    name="shell_command",
    description="执行 Shell 命令。输入要执行的命令字符串。",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 Shell 命令"}
        },
        "required": ["command"],
    },
    permission=ToolPermission.EXECUTE,
    category="system",
    max_frequency=30,
)
async def shell_command(command: str) -> ToolResult:
    """执行 Shell 命令（含危险命令拦截和输出敏感信息遮蔽）。

    优先通过虚空终端执行（用户可实时看到命令输入和输出），
    如果没有活跃终端会话则回退到 subprocess。
    """
    danger_reason = _is_command_dangerous(command)
    if danger_reason:
        return ToolResult.fail(danger_reason)

    # 尝试通过 PTY 终端执行
    try:
        from web.pty_executor import execute_on_pty
        from web.ws_hub import _pty_sessions, _pty_sessions_lock

        with _pty_sessions_lock:
            has_active_terminal = any(
                sess.get("alive") for sess in _pty_sessions.values()
            )

        if has_active_terminal:
            ok, output = await execute_on_pty("all", command, timeout=30.0)
            if ok:
                if output:
                    output = _sanitize_output(output)
                    return ToolResult.ok(output[:3000])
                return ToolResult.ok("命令执行成功（无输出）")
    except Exception:
        logger.debug("file_tools.pty_exec_error", exc_info=True)

    # Fallback: subprocess（无终端会话时）
    # 使用 asyncio.create_subprocess_shell 便于取消时 kill 子进程
    # （原 loop.run_in_executor + subprocess.run 无法被 asyncio.wait_for 取消，
    #  线程池中的子进程会继续运行直到自身超时，导致资源泄漏）
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser("~"),
        )
    except Exception as e:
        return ToolResult.fail(f"启动子进程失败: {e!s}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        out_text = (stdout.decode(errors="replace") if stdout else "") or (stderr.decode(errors="replace") if stderr else "")
        if out_text:
            out_text = _sanitize_output(out_text)
            data = out_text[:3000]
        else:
            data = "命令执行成功（无输出）"
        return ToolResult.ok(data)
    except asyncio.TimeoutError:
        # 超时后强制 kill 子进程，避免僵尸进程或后台泄漏
        # ProcessLookupError 表示子进程已自行退出，无需重复 kill
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logger.debug("file_tools.subprocess_kill_error", exc_info=True)
        # 修复：wait() 增加 5s 超时保护，防止子进程不退出时无限挂起
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("file_tools.subprocess_wait_timeout", hint="子进程 kill 后未在 5s 内退出，可能存在孤儿进程")
        except Exception:
            logger.debug("file_tools.subprocess_wait_error", exc_info=True)
        # 不在错误信息中包含 command，避免命令内容泄漏到日志/返回结果
        return ToolResult.fail("命令执行超时（30秒）")
    except Exception as e:
        # 兜底清理：子进程可能仍在运行
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logger.debug("file_tools.subprocess_kill_error", exc_info=True)
        # 修复：兜底清理同样增加 wait 超时保护
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("file_tools.subprocess_wait_timeout", hint="子进程 kill 后未在 5s 内退出，可能存在孤儿进程")
        except Exception:
            logger.debug("file_tools.subprocess_wait_error", exc_info=True)
        return ToolResult.fail(f"执行错误: {e!s}")


@register_tool(
    name="list_files",
    description="列出目录中的文件和文件夹。用于查看、整理或操作文件。输入目录路径，默认为当前目录。",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认 ~"}
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
)
def list_files(path: str = "~") -> ToolResult:
    """列出目录下的文件和文件夹，附带文件大小。"""
    try:
        target_path = os.path.expanduser(path)

        # 路径沙箱验证
        allowed, resolved, reason = _validate_path(target_path, mode="read")
        if not allowed:
            return ToolResult.fail(f"路径访问被拒绝: {reason}")

        if not os.path.exists(resolved):
            return ToolResult.fail(f"路径不存在: {path}")

        items = []
        for item in sorted(os.listdir(resolved)):
            full_path = os.path.join(resolved, item)
            is_dir = os.path.isdir(full_path)
            prefix = "📁" if is_dir else "📄"
            if is_dir:
                items.append(f"{prefix} {item}")
            else:
                try:
                    size = os.path.getsize(full_path)
                    for unit in ['B', 'KB', 'MB', 'GB']:
                        if size < 1024:
                            items.append(f"{prefix} {item} ({size:.1f}{unit})")
                            break
                        size /= 1024
                except OSError:
                    items.append(f"{prefix} {item}")

        return ToolResult.ok(f"目录: {resolved}\n" + "\n".join(items[:50]))
    except Exception as e:
        return ToolResult.fail(f"错误: {e!s}")


@register_tool(
    name="read_file",
    description="读取文件内容。输入文件路径。",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "offset": {"type": "integer", "description": "起始行号，默认0", "default": 0},
            "limit": {"type": "integer", "description": "读取行数，默认200", "default": 200}
        },
        "required": ["path"],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
)
def read_file(path: str, offset: int = 0, limit: int = 200) -> ToolResult:
    """读取指定文件的文本内容（支持行偏移和行数限制）。"""
    try:
        target_path = os.path.expanduser(path)

        # 路径沙箱验证
        allowed, resolved, reason = _validate_path(target_path, mode="read")
        if not allowed:
            return ToolResult.fail(f"路径访问被拒绝: {reason}")

        if not os.path.exists(resolved):
            return ToolResult.fail(f"文件不存在: {path}")
        if os.path.isdir(resolved):
            return ToolResult.fail(f"这是一个目录，不是文件: {path}")

        with _open_validated(resolved, mode="r", encoding="utf-8") as f:
            lines = f.readlines()
        selected = lines[offset:offset + limit]
        content = ''.join(selected)
        return ToolResult.ok(f"文件: {resolved}\n{'='*40}\n{content}")
    except PermissionError as e:
        return ToolResult.fail(f"路径访问被拒绝: {e!s}")
    except Exception as e:
        return ToolResult.fail(f"读取错误: {e!s}")


@register_tool(
    name="write_file",
    description="写入文件。输入格式: '文件路径|||内容'",
    schema={
        "type": "object",
        "properties": {
            "input_str": {"type": "string", "description": "格式: '文件路径|||内容'"}
        },
        "required": ["input_str"],
    },
    permission=ToolPermission.READ_WRITE,
    category="file",
    max_frequency=15,
)
def write_file(input_str: str) -> ToolResult:
    """将内容写入文件，输入格式为 '文件路径|||内容'。"""
    try:
        if '|||' not in input_str:
            return ToolResult.fail("格式错误。请使用: '文件路径|||内容'")

        path, content = input_str.split('|||', 1)
        target_path = os.path.expanduser(path)

        # 路径沙箱验证（写入模式）
        allowed, resolved, reason = _validate_path(target_path, mode="write")
        if not allowed:
            return ToolResult.fail(f"路径访问被拒绝: {reason}")

        os.makedirs(os.path.dirname(resolved), exist_ok=True)

        with _open_validated(resolved, mode="w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.ok(f"文件已写入: {resolved}")
    except PermissionError as e:
        return ToolResult.fail(f"路径访问被拒绝: {e!s}")
    except Exception as e:
        return ToolResult.fail(f"写入错误: {e!s}")


@register_tool(
    name="search_files",
    description="搜索文件。输入搜索模式（支持通配符），如 '*.py' 或 '/home/**/*.txt'",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "搜索模式，支持通配符"}
        },
        "required": ["pattern"],
    },
    permission=ToolPermission.READ_ONLY,
    category="file",
)
def search_files(pattern: str) -> ToolResult:
    """按通配符模式搜索文件，支持递归匹配。"""
    import glob
    try:
        expanded = os.path.expanduser(pattern)

        # 路径沙箱验证：提取目录部分进行验证
        dir_part = os.path.dirname(expanded)
        if not dir_part:
            dir_part = "."
        allowed, resolved_dir, reason = _validate_path(dir_part, mode="read")
        if not allowed:
            return ToolResult.fail(f"路径访问被拒绝: {reason}")

        # 使用验证后的目录重建搜索模式
        pattern_base = os.path.basename(expanded)
        safe_pattern = os.path.join(resolved_dir, pattern_base)

        matches = glob.glob(safe_pattern, recursive=True)
        if not matches:
            return ToolResult.fail(f"未找到匹配文件: {pattern}")

        result = [f"📄 {m}" for m in matches[:30]]
        if len(matches) > 30:
            result.append(f"... 还有 {len(matches) - 30} 个文件")
        return ToolResult.ok("\n".join(result))
    except Exception as e:
        return ToolResult.fail(f"搜索错误: {e!s}")
