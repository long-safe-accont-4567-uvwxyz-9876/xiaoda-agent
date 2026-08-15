"""TDD 测试：shell_command 危险命令强过滤（VULN-03）。

验证 _is_command_dangerous 拦截解释器执行、解码器管道等危险模式，
同时不误杀常见安全命令。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tools.file_tools_v2 import _is_command_dangerous


@pytest.mark.parametrize("command", [
    "python3 -c 'import os;os.system(\"id\")'",
    "python -m http.server",
    "perl -e 'system(\"id\")'",
    "ruby -e 'puts 1'",
    "node -e 'console.log(1)'",
    "awk 'BEGIN{system(\"id\")}'",
    "echo dGhpcyBpcyB0ZXN0 | base64 -d | bash",
    "echo 1234 | xxd -r -p | sh",
])
def test_dangerous_commands_blocked(command):
    """危险命令应被拦截。"""
    assert _is_command_dangerous(command) is not None, f"应拦截危险命令: {command!r}"


@pytest.mark.parametrize("command", [
    "systemctl status xxx",
    "echo hello",
])
def test_safe_commands_allowed(command):
    """安全命令不应被误杀。"""
    assert _is_command_dangerous(command) is None, f"不应拦截安全命令: {command!r}"
