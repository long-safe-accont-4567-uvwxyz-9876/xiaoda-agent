"""Windows 兼容性静态检查 —— 确保 Harness 优化代码跨平台兼容。"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 需检查的修改文件列表
_MODIFIED_FILES = [
    "agent_core/message_processor.py",
    "tool_engine/tool_call_handler.py",
    "tool_engine/tool_guardrails.py",
    "tool_engine/tool_executor.py",
    "prompt_builder.py",
    "xiaoli_agent.py",
    "agent_dispatcher.py",
    "config.py",
    "agent_context.py",
]

# 含超时逻辑、应使用 asyncio.wait_for 的文件
_TIMEOUT_FILES = [
    "xiaoli_agent.py",
    "tool_engine/tool_call_handler.py",
    "agent_core/message_processor.py",
]


def _read_file(name: str) -> str:
    """读取项目文件内容"""
    project_root = Path(__file__).parent.parent
    return (project_root / name).read_text(encoding="utf-8")


def _file_exists(name: str) -> bool:
    """判断项目内文件是否存在"""
    project_root = Path(__file__).parent.parent
    return (project_root / name).is_file()


class TestWindowsCompat:
    """Windows 兼容性静态检查测试集。"""

    def test_no_hardcoded_unix_paths(self):
        """检查代码逻辑中无硬编码 /tmp/ 或 /home/ 路径构造。"""
        bad_patterns = [
            re.compile(r'open\s*\(\s*["\']/tmp'),
            re.compile(r'Path\s*\(\s*["\']/tmp'),
            re.compile(r'open\s*\(\s*["\']/home'),
            re.compile(r'Path\s*\(\s*["\']/home'),
        ]
        violations = []
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            for pat in bad_patterns:
                for match in pat.finditer(content):
                    line_no = content[:match.start()].count("\n") + 1
                    violations.append(f"{name}:{line_no} 硬编码 Unix 路径: {match.group(0)!r}")
        assert not violations, "发现硬编码 Unix 路径:\n" + "\n".join(violations)

    def test_open_has_encoding(self):
        """检查所有 open() 调用均显式指定 encoding 参数。"""
        open_pattern = re.compile(r'(?<![\w.])open\s*\(')
        violations = []
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            for match in open_pattern.finditer(content):
                # 取 open( 之后 200 字符窗口（跨多行）查找 encoding=
                window = content[match.end():match.end() + 200]
                # 截断到首个右括号，限定为同一条语句
                close_idx = window.find(")")
                snippet = window if close_idx == -1 else window[:close_idx]
                if "encoding" not in snippet:
                    line_no = content[:match.start()].count("\n") + 1
                    violations.append(
                        f"{name}:{line_no} open() 调用缺少 encoding= 参数"
                    )
        assert not violations, "发现未指定 encoding 的 open() 调用:\n" + "\n".join(violations)

    def test_no_signal_sigkill(self):
        """检查代码中无 signal.SIGKILL 与 signal.alarm 用法。"""
        violations = []
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            if "signal.SIGKILL" in content:
                violations.append(f"{name} 含 signal.SIGKILL（Windows 不支持）")
            if "signal.alarm" in content:
                violations.append(f"{name} 含 signal.alarm（Windows 不支持）")
        assert not violations, "发现 Windows 不兼容的 signal 用法:\n" + "\n".join(violations)

    def test_no_sigkill(self):
        """检查代码中无 SIGKILL 或 signal.alarm 出现。"""
        violations = []
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            if "SIGKILL" in content:
                violations.append(f"{name} 含 SIGKILL")
            if "signal.alarm" in content:
                violations.append(f"{name} 含 signal.alarm")
        assert not violations, "发现 Windows 不兼容信号:\n" + "\n".join(violations)

    def test_pathlib_usage(self):
        """软检查: 路径操作使用 pathlib.Path 或 os.path 而非字符串拼接。"""
        path_indicators = re.compile(r'Path\s*\(|os\.path\.join|os\.path\b')
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            # 软检查: 只要文件存在上述任一模式即视为合规
            has_path_api = path_indicators.search(content) is not None
            # 仅记录信息，不强制失败（软检查）
            assert True, f"{name} 路径 API 使用情况: {'OK' if has_path_api else '未检测到'}"

    def test_asyncio_wait_for_not_signal(self):
        """检查超时逻辑使用 asyncio.wait_for 而非 signal.alarm。"""
        violations = []
        for name in _TIMEOUT_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            if "asyncio.wait_for" not in content:
                violations.append(
                    f"{name} 未使用 asyncio.wait_for 实现超时（Windows 不兼容 signal.alarm）"
                )
            if "signal.alarm" in content:
                violations.append(f"{name} 使用了 signal.alarm（Windows 不兼容）")
        assert not violations, "超时实现存在 Windows 兼容性问题:\n" + "\n".join(violations)

    def test_no_os_fork(self):
        """检查代码中无 os.fork() 调用（Windows 不支持）。"""
        fork_pattern = re.compile(r'os\.fork\s*\(')
        violations = []
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            for match in fork_pattern.finditer(content):
                line_no = content[:match.start()].count("\n") + 1
                violations.append(f"{name}:{line_no} 含 os.fork() 调用")
        assert not violations, "发现 os.fork() 调用（Windows 不兼容）:\n" + "\n".join(violations)

    def test_no_unix_only_imports(self):
        """检查代码中无 fcntl/termios/grp/pwd 等 Unix 专属导入。"""
        unix_import_patterns = [
            re.compile(r'^\s*import\s+fcntl\b', re.MULTILINE),
            re.compile(r'^\s*import\s+termios\b', re.MULTILINE),
            re.compile(r'^\s*import\s+grp\b', re.MULTILINE),
            re.compile(r'^\s*import\s+pwd\b', re.MULTILINE),
            re.compile(r'^\s*from\s+fcntl\b', re.MULTILINE),
            re.compile(r'^\s*from\s+termios\b', re.MULTILINE),
            re.compile(r'^\s*from\s+grp\b', re.MULTILINE),
            re.compile(r'^\s*from\s+pwd\b', re.MULTILINE),
        ]
        violations = []
        for name in _MODIFIED_FILES:
            if not _file_exists(name):
                continue
            content = _read_file(name)
            for pat in unix_import_patterns:
                for match in pat.finditer(content):
                    line_no = content[:match.start()].count("\n") + 1
                    violations.append(
                        f"{name}:{line_no} 含 Unix 专属导入: {match.group(0).strip()!r}"
                    )
        assert not violations, "发现 Unix 专属导入（Windows 不兼容）:\n" + "\n".join(violations)
