"""TDD 测试：文件沙箱敏感路径加固（VULN-05 / VULN-06）。

覆盖：
- 凭证目录与敏感文件拒绝（~/.ai-agent/credentials）
- 家目录敏感文件拉黑（.bashrc / .profile / .gitconfig / .docker / .ssh）
- 临时目录白名单不回归
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tools.file_tools_v2 import _validate_path


@pytest.mark.parametrize("path", [
    os.path.expanduser("~/.ai-agent/credentials/webui_secret"),
    os.path.expanduser("~/.ai-agent/credentials/provider_xxx.key"),
])
def test_credentials_dir_rejected(path):
    """凭证目录及其下文件应被拒绝（即使 ~ 在白名单内）。"""
    allowed, _resolved, reason = _validate_path(path, "read")
    assert allowed is False, f"凭证路径应被拒绝: {reason}"


@pytest.mark.parametrize("path,mode", [
    (os.path.expanduser("~/.bashrc"), "write"),
    (os.path.expanduser("~/.profile"), "write"),
    (os.path.expanduser("~/.gitconfig"), "read"),
    (os.path.expanduser("~/.docker/config.json"), "read"),
    (os.path.expanduser("~/.ssh/id_rsa"), "read"),
])
def test_sensitive_home_files_rejected(path, mode):
    """家目录敏感文件应被拉黑。"""
    allowed, _resolved, reason = _validate_path(path, mode)
    assert allowed is False, f"{path} 应被拒绝: {reason}"


def test_tmp_write_still_allowed():
    """临时目录写入不回归。"""
    allowed, _resolved, reason = _validate_path("/tmp/foo.txt", "write")
    assert allowed is True, f"/tmp 写入应被允许: {reason}"
